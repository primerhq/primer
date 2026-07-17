"""Service-layer helper for resuming a parked session.

Extracted from the yield-respond REST endpoints so the parked_session
trigger dispatcher (Plan §5.4) can reach the same wake path without
going through HTTP. Spec §5.4.

The yielding-tools surface parks a session on
``sessions.parked_state`` (or ``chats.parked_state``); the row carries
an ``event_key`` inside ``parked_state['yielded']``. Resuming the
yield consists of publishing a payload onto that key — the event bus
listener inside each worker pool catches the publish and atomically
flips ``parked_status`` from ``"parked"`` to ``"resumable"`` so the
next claim loop picks the row up.

The router endpoints in :mod:`primer.api.routers.yields` and
:mod:`primer.api.routers.tool_approval` do this inline today. This
helper consolidates the lookup + validation + publish so:

* The parked_session dispatcher can call it directly from inside the
  trigger fire worker.
* Future yielding-tool resume callers (e.g. MCP bridge, in-process
  unit tests) don't have to re-implement the parked_state walk.

The helper is intentionally tolerant of both Session and Chat parks
— the parked_state shape is identical across both entities — but the
trigger dispatcher only targets workspace sessions today.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from primer.int.claim import ClaimKind
from primer.model.except_ import NotFoundError
from primer.model.workspace_session import WorkspaceSession

if TYPE_CHECKING:
    from primer.int.claim import ClaimEngine
    from primer.int.storage import Storage


logger = logging.getLogger(__name__)


async def durably_mark_session_resumable(
    session: WorkspaceSession,
    *,
    event_key: str,
    payload: dict[str, Any] | None,
    session_storage: "Storage[WorkspaceSession]",
    engine: "ClaimEngine | None",
) -> bool:
    """Guarded, durable ``parked -> resumable`` flip for one session row.

    This is the single source of truth for the park->resumable transition,
    shared by the bus listener (``primer.bus.listener.YieldEventListener``,
    which reacts to a bus NOTIFY) and the REST reply handlers (which now
    perform it durably so a listener outage cannot silently drop an operator
    reply — see arch review D-C2).

    Steps, mirroring the listener's original ``_flip_rows`` write exactly:

    * Stamp the singular ``resume_event_payload`` / ``resume_event_key`` (the
      single-event resume path + a "last fired" hint).
    * For a MULTI-event park (``parked_event_keys`` set) also accumulate
      ``resume_event_payloads[fired_tcid]`` so a second concurrent reply is
      preserved rather than overwritten.
    * ``storage.update`` the flipped row.
    * Re-arm the claim lease via ``engine.mark_resumable`` (park dropped it)
      so the claim loop re-claims the row WITHOUT relying on any bus. When no
      engine is wired (e.g. the lightweight test app) the durable storage
      flip still lands; the lease re-arm is simply skipped.

    Idempotency (the listener may also process the NOTIFY): a single-event
    park only advances from ``parked``, so a second flip is a no-op; a
    multi-event park may advance from ``resumable`` and re-accumulates the
    same ``fired_tcid`` with identical data. Returns True when the row was
    advanced/accumulated, False when the guard rejected it.
    """
    is_multi = bool(session.parked_event_keys)
    allowed = ("parked", "resumable") if is_multi else ("parked",)
    if session.parked_status not in allowed:
        return False
    state = dict(session.parked_state or {})
    # Singular fields: the single-event resume path + a "last fired" hint.
    state["resume_event_payload"] = dict(payload or {})
    state["resume_event_key"] = event_key
    if is_multi:
        fired_tcid = event_key.rsplit(":", 1)[-1]
        payloads = dict(state.get("resume_event_payloads") or {})
        payloads[fired_tcid] = {
            "payload": dict(payload or {}),
            "event_key": event_key,
        }
        state["resume_event_payloads"] = payloads
    updated = session.model_copy(update={
        "parked_status": "resumable",
        "parked_state": state,
    })
    await session_storage.update(updated)
    # Re-arm the engine lease (park dropped it). mark_resumable upserts a
    # fresh claimable lease when none exists.
    if engine is not None:
        await engine.mark_resumable(ClaimKind.SESSION, session.id)
    return True


async def durably_wake_session(
    session: WorkspaceSession,
    *,
    event_key: str,
    payload: dict[str, Any] | None,
    session_storage: "Storage[WorkspaceSession]",
    engine: "ClaimEngine | None",
) -> bool:
    """Durable flip for the REST reply handlers, repairing a missing lease.

    :func:`durably_mark_session_resumable` writes twice and the two writes
    CANNOT share a transaction (``mark_resumable`` acquires its own
    connection). So a crash between them leaves the row ``resumable`` with
    NO lease row - and ``claim_due`` JOINs the leases table, which makes the
    session permanently unclaimable. The reply handlers must not report the
    reply accepted in that state.

    This wrapper ACTS on the helper's return value instead of discarding it:
    a False return on a row whose ``parked_status`` is already ``resumable``
    is exactly the fingerprint of that half-applied flip (the guard only
    admits ``parked`` for a single-event park), so re-drive
    ``mark_resumable`` - an idempotent upsert - to re-create the lease the
    first attempt lost. When the lease is already healthy the upsert is a
    harmless no-op, which is the common case for an ordinary double-reply.

    A raising ``storage.update`` still propagates untouched: the caller must
    NOT report a reply accepted when the durable stamp never landed.

    Returns the underlying helper's bool (True when this call advanced the
    row, False when the guard rejected it).
    """
    did = await durably_mark_session_resumable(
        session,
        event_key=event_key,
        payload=payload,
        session_storage=session_storage,
        engine=engine,
    )
    if did or engine is None:
        return did
    if session.parked_status != "resumable":
        # Guard rejected for some other reason (not a half-applied flip);
        # there is no lease to repair.
        return did
    logger.info(
        "Repairing claim lease for session %s: the row is already "
        "'resumable' but the durable flip may not have re-armed its lease",
        session.id,
    )
    await engine.mark_resumable(ClaimKind.SESSION, session.id)
    return did


@dataclass
class RespondToYieldDeps:
    """Collaborators :func:`respond_to_yield` needs.

    Kept tiny on purpose — the helper does one storage lookup and one
    bus publish; everything else lives inside the worker pool's bus
    listener and the resume-classifier in
    :mod:`primer.worker.yield_runtime`.
    """

    storage_provider: Any
    event_bus: Any


def _tool_call_id_for(blob: dict[str, Any]) -> str | None:
    """Resolve tool_call_id from a parked_state blob.

    Mirrors :func:`primer.api.routers.yields._tool_call_id_for`. Worker
    writes it at the top level; older parks may have only had it inside
    ``yielded.resume_metadata``. Falling back keeps the lookup robust
    across upgrades.
    """
    tcid = blob.get("tool_call_id")
    if tcid:
        return tcid
    yielded = blob.get("yielded") or {}
    metadata = yielded.get("resume_metadata") or {}
    return metadata.get("tool_call_id")


async def respond_to_yield(
    *,
    session_id: str,
    tool_call_id: str,
    result: Any,
    deps: RespondToYieldDeps,
) -> None:
    """Publish *result* onto the parked session's resume ``event_key``.

    Steps:

    1. Look up the :class:`WorkspaceSession` row.
    2. Validate the row is parked (or already resumable) and that its
       in-flight ``tool_call_id`` matches.
    3. Pull ``event_key`` out of the parked_state blob.
    4. Publish ``result`` onto that key via the event bus. The bus
       listener inside the worker pool flips the row to ``resumable``.

    Raises
    ------
    NotFoundError
        When the session doesn't exist, isn't parked, or is parked on
        a different ``tool_call_id``.

    Notes
    -----
    The helper does NOT write to ``parked_state`` itself — the worker
    pool's bus listener owns that flip via the scheduler's atomic
    ``mark_resumable``. Calling this helper twice for the same yield is
    a no-op once the first publish has flipped the row to ``resumable``
    (the second publish goes onto the bus too, but ``mark_resumable``
    is idempotent so duplicate flips are harmless).
    """
    storage = deps.storage_provider.get_storage(WorkspaceSession)
    session = await storage.get(session_id)
    if session is None:
        raise NotFoundError(f"Session {session_id!r} does not exist")

    if session.parked_status not in ("parked", "resumable"):
        raise NotFoundError(
            f"Session {session_id!r} has no in-flight yield to resume"
        )
    blob: dict[str, Any] = session.parked_state or {}
    expected = _tool_call_id_for(blob)
    if expected != tool_call_id:
        raise NotFoundError(
            f"No in-flight yield with tool_call_id {tool_call_id!r} "
            f"on session {session_id!r}"
        )

    yielded: dict[str, Any] = blob.get("yielded") or {}
    event_key: str | None = yielded.get("event_key")
    if not event_key:
        # Defensive — every park written by the current runtime carries
        # an event_key. A missing one means a corrupted park; the
        # caller's only sensible recourse is to surface 404 like the
        # REST endpoint does.
        raise NotFoundError(
            f"Session {session_id!r} park is missing event_key"
        )

    payload: dict[str, Any]
    if isinstance(result, dict):
        payload = result
    else:
        payload = {"response": result}
    await deps.event_bus.publish(event_key, payload)


__all__ = [
    "RespondToYieldDeps",
    "durably_mark_session_resumable",
    "durably_wake_session",
    "respond_to_yield",
]
