"""Subscription dispatcher registry + shared types.

Spec §5: each subscription kind has its own dispatcher that knows how
to deliver a fired trigger into the right downstream artefact (a chat
turn, a fresh workspace session, a yielding-tool resume). The
dispatchers share a result envelope and a deps bundle so the fire
orchestrator (Phase 6) treats them uniformly.

Kind-specific dispatchers live in sibling modules
(``chat_message.py``, ``agent_fresh_session.py``,
``graph_fresh_session.py``, ``parked_session.py``) and self-register at
import time by calling :func:`register`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel

from primer.model.trigger import Subscription


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
    every dispatcher uses them -- ``chat_message`` doesn't need either,
    ``parked_session`` reaches for the event bus, the fresh-session
    dispatchers may want the workspace registry for slot allocation.
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


__all__ = [
    "DISPATCHERS",
    "DispatchDeps",
    "Dispatcher",
    "SubscriptionDispatchResult",
    "get_dispatcher",
    "register",
]
