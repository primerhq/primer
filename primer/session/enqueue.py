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
  * ENDED             -> restart (reopen + invocation divider, then invoke)

Uniform "send a message" semantics: every clean agent turn ends the session,
and sending a NEW message to an ENDED session reopens it (reopen the on-disk
slot, write an invocation divider, run a fresh turn) instead of erroring. A
non-restartable ``ended_reason`` (``workspace_lost``/``force_deleted``) still
raises ConflictError.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from primer.int.claim import ClaimKind
from primer.model.except_ import NotFoundError
from primer.model.workspace_session import (
    SessionMessageKind,
    SessionMessageRecord,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.mutation_lock import session_lifecycle_lock
from primer.session.persistence import WorkspaceMessageWriter
from primer.session.reset import _reopen_ended_locked

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

    Raises NotFoundError (missing / workspace mismatch). An ENDED session is
    reopened in place (reopen slot + invocation divider + ENDED->CREATED)
    before the normal invoke flow, so a new message restarts it; a
    non-restartable ``ended_reason`` (``workspace_lost``/``force_deleted``)
    still raises ConflictError. Serialised against concurrent
    cancel/pause/resume via the session lifecycle lock so the ``turn_status``
    flip + status transition never interleave with a racing cancel's ENDED
    write.
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
            # Reopen the ended session in place so a NEW message restarts it
            # (reopen slot + invocation divider + ENDED->CREATED). We hold the
            # non-reentrant session_lifecycle_lock, and reset_session acquires
            # the SAME lock -- calling it here would deadlock -- so we share
            # its lock-free reopen body via _reopen_ended_locked. The helper
            # raises ConflictError for non-restartable ended_reasons. After
            # this, row is CREATED and the normal wake flow below appends the
            # USER_INPUT (after the divider) and runs the turn.
            row, _invocation = await _reopen_ended_locked(
                row=row,
                workspace_id=workspace_id,
                session_id=session_id,
                sessions=sessions,
                workspace_registry=deps.workspace_registry,
            )
            # Surface the invocation divider live before the USER_INPUT tick
            # below (mirrors reset_session's divider tick). Advisory.
            if deps.event_bus is not None:
                try:
                    await deps.event_bus.publish(
                        f"session:{session_id}:tick", {"seq": row.last_seq}
                    )
                except Exception:  # noqa: BLE001 -- advisory
                    logger.exception(
                        "wake_session: failed to publish divider tick for %s",
                        session_id,
                    )

        # 1. Append the user message to the on-disk slot FIFO (the queue) and
        #    persist a USER_INPUT record to messages.jsonl so the sent message
        #    shows in the session transcript. The slot FIFO is the worker's
        #    queue; messages.jsonl is the display log — separate surfaces, so
        #    the USER_INPUT record is written even when the slot is absent
        #    (mirrors the chat lane's append_user_message + reset_session's
        #    divider write via WorkspaceMessageWriter). Exactly one record per
        #    supplied instruction, so a steer/invoke never double-records.
        user_input_seq: int | None = None
        if instruction:
            ws = await deps.workspace_registry.get_workspace(workspace_id)
            slot = await ws.get_session(session_id)
            if slot is not None:
                await slot.append_instruction(instruction)
            writer = WorkspaceMessageWriter(
                workspace_io=ws, session_id=session_id, start_seq=row.last_seq,
            )
            user_input_seq = await writer.append(SessionMessageRecord(
                seq=1,  # overwritten by the writer's monotonic counter
                kind=SessionMessageKind.USER_INPUT,
                payload={"text": instruction},
                created_at=datetime.now(timezone.utc),
            ))
            await writer.flush()

        # 2. Re-arm the scheduler-visible row: claimable + clear pause;
        #    CREATED/PAUSED/WAITING advance to RUNNING (like /resume).
        row.turn_status = "claimable"
        row.pause_requested = False
        row.pause_requested_at = None
        if user_input_seq is not None:
            row.last_seq = user_input_seq
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
            # Tick so the workspace tap surfaces the just-persisted USER_INPUT
            # record live (reset_session publishes the same event for its
            # invocation divider). Advisory — never break the wake.
            if user_input_seq is not None:
                try:
                    await deps.event_bus.publish(
                        f"session:{session_id}:tick", {"seq": user_input_seq}
                    )
                except Exception:  # noqa: BLE001 -- advisory
                    logger.exception(
                        "wake_session: failed to publish tick for %s",
                        session_id,
                    )
        return row


__all__ = ["SessionWakeDeps", "wake_session"]
