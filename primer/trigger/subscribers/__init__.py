"""Subscription dispatcher registry + shared types.

Spec §5: each subscription kind has its own dispatcher that knows how
to deliver a fired trigger into the right downstream artefact (a chat
turn, a fresh workspace session, a yielding-tool resume). The
dispatchers share a result envelope and a deps bundle so the fire
orchestrator (Phase 6) treats them uniformly.

Kind-specific dispatchers live in sibling modules
(``agent_fresh_session.py``, ``graph_fresh_session.py``,
``parked_session.py``, ``session_append.py``) and self-register at
import time by calling :func:`register`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from pydantic import BaseModel

from primer.model.storage import Op, OffsetPage
from primer.model.trigger import Subscription
from primer.model.workspace_session import SessionStatus, WorkspaceSession
from primer.storage.q import Q


#: How long a session that has never run a turn may hold a ``parallelism="skip"``
#: subscription closed. A session is created with ``auto_start=True`` and its first turn
#: is claimed moments later; if the claim is lost (the worker died, the node OOM-killed it)
#: the row stays non-terminal at ``turn_no == 0`` and nothing ever reaps it. Without a bound
#: that one row wedges its subscription permanently - every later fire skips, silently, for
#: as long as the row exists.
SKIP_GATE_START_GRACE = timedelta(minutes=10)


class SubscriptionDispatchResult(BaseModel):
    """Result envelope returned by every dispatcher.

    ``ok`` is ``True`` for both happy-path delivery AND for skips that
    were a deliberate no-op (e.g. ``parallelism="skip"`` with a busy
    target). ``skipped`` distinguishes those two cases. ``error_code``
    is a short machine-readable token (see the per-dispatcher list in
    the spec); ``error_message`` is the human-readable refinement.
    ``artefact_id`` carries the id of whatever the dispatcher created
    (a chat message, a workspace session, ...).
    """

    ok: bool
    skipped: bool = False
    error_code: str | None = None
    error_message: str | None = None
    artefact_id: str | None = None


@dataclass
class DispatchDeps:
    """Collaborators the dispatchers may need.

    ``storage_provider`` and ``claim_engine`` are load-bearing for the
    fresh-session dispatchers (``agent_fresh_session`` /
    ``graph_fresh_session``), which create an ``auto_start=True`` session.
    ``claim_engine`` is therefore typed ``Any`` (NOT ``Any | None``) and
    has no default: a ``None`` here lets a fresh session flip to RUNNING
    with no claimer and hang forever. The webhook background-task path
    historically passed ``claim_engine=None`` -- that is fixed (it now
    threads the live engine from ``app.state``), and
    :func:`primer.workspace.session_factory.create_session` raises
    ``ConfigError`` as the runtime backstop for any path that still
    forgets it.

    ``scheduler`` is legitimately optional: the channel-event and
    manual-fire paths (``primer.trigger.service.fire_trigger_now``,
    ``primer.channel.inbound_router``) pass ``scheduler=None`` because
    they do not drive the scheduler -- the ClaimEngine upsert is the
    worker's wake-up path there, and ``create_session`` swallows the
    absent-scheduler enqueue best-effort.

    ``workspace_registry`` and ``event_bus`` are optional because not
    every dispatcher uses them -- ``parked_session`` reaches for the event
    bus, the fresh-session and ``session_append`` dispatchers want the
    workspace registry for slot allocation.
    """

    storage_provider: Any
    claim_engine: Any
    scheduler: Any | None = None
    workspace_registry: Any | None = None
    event_bus: Any | None = None


class Dispatcher(Protocol):
    """Structural type for subscription dispatchers."""

    async def dispatch(
        self,
        sub: Subscription,
        *,
        rendered_payload: str,
        fire_context: dict,
        fire_id: str,
        deps: DispatchDeps,
    ) -> SubscriptionDispatchResult: ...


DISPATCHERS: dict[str, Dispatcher] = {}


def register(kind: str, dispatcher: Dispatcher) -> None:
    """Register *dispatcher* under *kind*.

    Sibling modules call this at import time. The fire orchestrator
    looks dispatchers up via :func:`get_dispatcher`.
    """
    DISPATCHERS[kind] = dispatcher


def get_dispatcher(kind: str) -> Dispatcher:
    """Return the dispatcher registered for *kind*.

    Raises ``KeyError`` for unknown kinds — the caller is expected to
    surface that as a structured fire error.
    """
    if kind not in DISPATCHERS:
        raise KeyError(f"unknown subscription kind: {kind!r}")
    return DISPATCHERS[kind]


def session_holds_skip_gate(
    session: WorkspaceSession, *, now: datetime | None = None,
) -> bool:
    """Whether *session* should keep a ``parallelism="skip"`` subscription from firing.

    Only sessions that are plausibly still doing work hold the gate:

    * ``ENDED`` never holds it.
    * A cancelled-requested session never holds it. Cancel is a flag the worker reads on
      its next step, so a session that never started has nobody to read it; without this
      an operator asking to cancel could not reopen the gate at all.
    * ``turn_no >= 1`` always holds it, for as long as the turn runs. A started turn may
      legitimately take hours (a graph build, a long exec), and firing a second session
      beside it is worse than skipping - two runs writing the same workspace state can
      clobber each other. Elapsed time is deliberately NOT consulted here.
    * ``turn_no == 0`` holds it only within :data:`SKIP_GATE_START_GRACE`. The first turn
      is claimed moments after creation, so a row still at turn 0 well past that lost its
      claim and will never run.
    """
    if session.status == SessionStatus.ENDED:
        return False
    if session.cancel_requested:
        return False
    if session.turn_no > 0:
        return True
    ref = session.started_at or session.created_at
    if ref is None:  # defensive: unset clock -> treat as live rather than fire twice
        return True
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    return ((now or datetime.now(timezone.utc)) - ref) < SKIP_GATE_START_GRACE


async def check_subscription_busy(
    sub: Subscription,
    deps: DispatchDeps,
    *,
    fire_context: dict | None = None,
) -> SubscriptionDispatchResult | None:
    """Return a ``skipped`` result if a live session attributed to *sub* exists, else ``None``.

    Shared by the agent_fresh and graph_fresh dispatchers so the busy-check
    semantics stay identical; liveness is decided by
    :func:`session_holds_skip_gate`.

    A fire carrying an inbound channel event is EXEMPT (crosscheck M7):
    channel dispatch is thread-mapped, so a fire only reaches a fresh-session
    subscriber when the thread has no session yet. Skipping there would
    silently drop the user's first message in a brand-new thread.
    """
    if fire_context is not None and fire_context.get("event") is not None:
        return None
    sessions = deps.storage_provider.get_storage(WorkspaceSession)
    predicate = (
        Q(WorkspaceSession)
        .where_op("metadata.subscription_id", Op.EQ, sub.id)
        .build()
    )
    page = await sessions.find(predicate, OffsetPage(offset=0, length=200))
    for s in page.items:
        if session_holds_skip_gate(s):
            return SubscriptionDispatchResult(
                ok=True,
                skipped=True,
                error_code="skipped_subscription_busy",
                error_message=f"session {s.id!r} still in-flight",
            )
    return None


__all__ = [
    "DISPATCHERS",
    "SKIP_GATE_START_GRACE",
    "DispatchDeps",
    "Dispatcher",
    "SubscriptionDispatchResult",
    "check_subscription_busy",
    "get_dispatcher",
    "register",
    "session_holds_skip_gate",
]
