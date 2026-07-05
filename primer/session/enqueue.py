"""Session auto-wake — the keystone that unifies invoke = steer = resume.

An inbound user message re-arms a session's scheduler claim so the worker
runs a fresh turn, regardless of the session's current lifecycle state.
Mirrors ``primer/chat/enqueue.py`` + ``send_chat_message``'s persist+wake
tail: the session's on-disk ``messages.jsonl`` IS the FIFO queue
(``AgentSession.append_instruction``); the scheduler-visible
``WorkspaceSession`` row carries the claim state (``turn_status`` +
``ClaimEngine`` lease + scheduler enqueue).

Behaviour by status (studio-agents-interact §4.2 / §5.1):
  * CREATED           -> invoke  (transition to RUNNING; run with the message)
  * RUNNING / WAITING -> steer   (queue as the next turn; re-arm the claim)
  * PAUSED            -> resume  (clear pause; transition to RUNNING)
  * ENDED             -> ConflictError (use restart / reset_session first)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from primer.int.claim import ClaimKind
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.workspace_session import SessionStatus, WorkspaceSession
from primer.session.mutation_lock import session_lifecycle_lock

logger = logging.getLogger(__name__)

_RESUMABLE = {
    SessionStatus.CREATED,
    SessionStatus.PAUSED,
    SessionStatus.WAITING,
}


@dataclass
class SessionWakeDeps:
    """Collaborators :func:`wake_session` needs.

    ``event_bus`` is optional: when present a ``session:{sid}:claimable``
    event is published for observability (the scheduler enqueue + claim
    upsert are the load-bearing pulse). ``workspace_registry`` is required
    when an ``instruction`` is supplied (to reach the on-disk slot's FIFO).
    """

    storage_provider: Any
    scheduler: Any
    claim_engine: Any
    workspace_registry: Any
    event_bus: Any | None = None


async def wake_session(
    *,
    workspace_id: str,
    session_id: str,
    instruction: str | None,
    deps: SessionWakeDeps,
) -> WorkspaceSession:
    """Append ``instruction`` (if any) and re-arm the session's claim.

    Raises NotFoundError (missing / workspace mismatch) / ConflictError
    (already ENDED). Serialised against concurrent cancel/pause/resume via
    the session lifecycle lock so the ``turn_status`` flip + status
    transition never interleave with a racing cancel's ENDED write.
    """
    sessions = deps.storage_provider.get_storage(WorkspaceSession)
    async with session_lifecycle_lock().acquire(session_id):
        row = await sessions.get(session_id)
        if row is None or row.workspace_id != workspace_id:
            raise NotFoundError(
                f"Session {session_id!r} does not exist on workspace "
                f"{workspace_id!r}"
            )
        if row.status == SessionStatus.ENDED:
            raise ConflictError(
                f"Session {session_id!r} has ended; restart it to re-open."
            )

        # 1. Append the user message to the on-disk slot FIFO (the queue).
        if instruction:
            ws = await deps.workspace_registry.get_workspace(workspace_id)
            slot = await ws.get_session(session_id)
            if slot is not None:
                await slot.append_instruction(instruction)

        # 2. Re-arm the scheduler-visible row: claimable + clear pause;
        #    CREATED/PAUSED/WAITING advance to RUNNING (like /resume).
        row.turn_status = "claimable"
        row.pause_requested = False
        row.pause_requested_at = None
        if row.status in _RESUMABLE:
            row.status = SessionStatus.RUNNING
            if row.started_at is None:
                row.started_at = datetime.now(timezone.utc)
        await sessions.update(row)

        # 3. Pulse the scheduler + claim engine so a worker runs the turn.
        await deps.scheduler.enqueue(session_id)
        if deps.claim_engine is not None:
            await deps.claim_engine.upsert(ClaimKind.SESSION, session_id)
        if deps.event_bus is not None:
            try:
                await deps.event_bus.publish(
                    f"session:{session_id}:claimable", {}
                )
            except Exception:  # noqa: BLE001 -- never break the wake
                logger.exception(
                    "wake_session: failed to publish claimable for %s",
                    session_id,
                )
        return row


__all__ = ["SessionWakeDeps", "wake_session"]
