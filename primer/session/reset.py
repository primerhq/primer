"""Reset-same-session: re-open an ENDED session as a fresh invocation.

A session is a persistent interactive entity (studio-agents-interact §5.2).
``reset_session`` transitions the scheduler-visible row ENDED -> CREATED,
clears the terminal + park bookkeeping, reopens the on-disk slot, and
appends an "invocation N" divider to the append-only ``messages.jsonl`` so
repeated invocations live in one continuous, legible stream. It does NOT
invoke; ``restart_session`` (Task 7) chains reset + wake.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from primer.model.except_ import ConflictError, NotFoundError
from primer.model.workspace_session import (
    SessionMessageKind,
    SessionMessageRecord,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.mutation_lock import session_lifecycle_lock
from primer.session.persistence import WorkspaceMessageWriter

logger = logging.getLogger(__name__)

# ended_reasons that are safe to re-open (studio-agents-interact §5.3).
_RESTARTABLE = {"completed", "failed", "cancelled"}


@dataclass
class SessionResetDeps:
    storage_provider: Any
    workspace_registry: Any
    event_bus: Any | None = None


async def _reopen_ended_locked(
    *,
    row: WorkspaceSession,
    workspace_id: str,
    session_id: str,
    sessions: Any,
    workspace_registry: Any,
) -> tuple[WorkspaceSession, int]:
    """Reopen an ENDED session row IN PLACE — lock-free reopen body.

    Shared by :func:`reset_session` and ``wake_session``'s ENDED branch.
    Both callers already hold ``session_lifecycle_lock().acquire(session_id)``
    (a non-reentrant lock), so this helper must NOT re-acquire it — calling
    ``reset_session`` from inside ``wake_session``'s lock would deadlock.

    Assumes the caller verified ``row.status == SessionStatus.ENDED``.
    Enforces the ``_RESTARTABLE`` ended_reason guard here (raising
    ConflictError for ``workspace_lost``/``force_deleted``), reopens the
    on-disk slot, writes the ``INVOCATION_DIVIDER`` record, bumps the
    invocation counter, clears terminal + park bookkeeping, and transitions
    the scheduler row ENDED -> CREATED (turn_status idle). Returns
    ``(reopened_row, invocation_number)``; does NOT publish events (the
    caller owns ticks). The divider is written BEFORE the caller appends any
    USER_INPUT so the invocation divider precedes the message.
    """
    if row.ended_reason not in _RESTARTABLE:
        raise ConflictError(
            f"Session {session_id!r} ended as {row.ended_reason!r} and "
            "cannot be re-opened."
        )

    invocation = int(row.metadata.get("invocation", 1)) + 1

    # 1. Reopen the on-disk slot (ENDED -> RUNNING; sanctioned exception).
    ws = await workspace_registry.get_workspace(workspace_id)
    slot = await ws.get_session(session_id)
    if slot is not None:
        await slot.reopen()

    # 2. Append the invocation divider to messages.jsonl, seeded past
    #    existing history so seqs stay monotonic.
    writer = WorkspaceMessageWriter(
        workspace_io=ws, session_id=session_id, start_seq=row.last_seq,
    )
    new_seq = await writer.append(SessionMessageRecord(
        seq=1,  # overwritten by the writer's counter
        kind=SessionMessageKind.INVOCATION_DIVIDER,
        payload={"invocation": invocation},
        created_at=datetime.now(timezone.utc),
    ))
    await writer.flush()

    # 3. Re-open the scheduler row: ENDED -> CREATED, clear terminal +
    #    park bookkeeping; bump the invocation counter + last_seq.
    md = dict(row.metadata)
    md["invocation"] = invocation
    reopened = row.model_copy(update={
        "status": SessionStatus.CREATED,
        "ended_reason": None,
        "ended_at": None,
        "cancel_requested": False,
        "cancel_requested_at": None,
        "pause_requested": False,
        "pause_requested_at": None,
        "interrupt_requested": False,
        "parked_status": None,
        "parked_event_key": None,
        "parked_event_keys": None,
        "parked_until": None,
        "parked_at": None,
        "parked_state": None,
        "turn_status": "idle",
        "last_seq": new_seq,
        "metadata": md,
    })
    await sessions.update(reopened)
    return reopened, invocation


async def reset_session(
    *,
    workspace_id: str,
    session_id: str,
    deps: SessionResetDeps,
) -> tuple[WorkspaceSession, int]:
    """Re-open an ENDED session and append an invocation divider.

    Returns ``(row, invocation_number)``. Raises NotFoundError (missing /
    workspace mismatch) / ConflictError (not ENDED, or a non-restartable
    ``ended_reason`` such as ``workspace_lost``/``force_deleted``).
    """
    sessions = deps.storage_provider.get_storage(WorkspaceSession)
    async with session_lifecycle_lock().acquire(session_id):
        row = await sessions.get(session_id)
        if row is None or row.workspace_id != workspace_id:
            raise NotFoundError(
                f"Session {session_id!r} does not exist on workspace "
                f"{workspace_id!r}"
            )
        if row.status != SessionStatus.ENDED:
            raise ConflictError(
                f"Session {session_id!r} is not ENDED (status "
                f"{row.status.value}); reset only re-opens ended sessions."
            )

        reopened, invocation = await _reopen_ended_locked(
            row=row,
            workspace_id=workspace_id,
            session_id=session_id,
            sessions=sessions,
            workspace_registry=deps.workspace_registry,
        )

        if deps.event_bus is not None:
            try:
                await deps.event_bus.publish(
                    f"session:{session_id}:tick", {"seq": reopened.last_seq}
                )
            except Exception:  # noqa: BLE001 -- advisory
                logger.exception(
                    "reset_session: tick publish failed for %s", session_id,
                )
        return reopened, invocation


async def restart_session(
    *,
    workspace_id: str,
    session_id: str,
    instruction: str | None,
    reset_deps: "SessionResetDeps",
    wake_deps,  # primer.session.enqueue.SessionWakeDeps
) -> WorkspaceSession:
    """Reset an ENDED session then invoke it (studio-agents-interact §5.3).

    Restart = reset-same-session (§5.2) + auto-wake (§5.1). Works for
    completed / failed / cancelled sessions; a non-ENDED (active) session
    is rejected by ``reset_session`` with a ConflictError (the restart 409
    gate). ``reset_session`` moves the row ENDED->CREATED first, so the
    subsequent ``wake_session`` sees CREATED and merely invokes — its ENDED
    reopen branch never fires here, so there is exactly one invocation
    divider (no double-reopen).
    """
    from primer.session.enqueue import wake_session

    await reset_session(
        workspace_id=workspace_id, session_id=session_id, deps=reset_deps,
    )
    return await wake_session(
        workspace_id=workspace_id,
        session_id=session_id,
        instruction=instruction,
        deps=wake_deps,
    )


__all__ = [
    "SessionResetDeps",
    "_reopen_ended_locked",
    "reset_session",
    "restart_session",
]
