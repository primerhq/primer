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
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from primer.model.except_ import ConflictError, NotFoundError
from primer.model.workspace_session import (
    PeerAttribution,
    SessionMessageKind,
    SessionMessageRecord,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.mutation_lock import session_lifecycle_lock
from primer.session.persistence import WorkspaceMessageWriter

logger = logging.getLogger(__name__)

# ended_reasons that are safe to re-open (studio-agents-interact §5.3). ``tool_turn_cap`` (an autonomous
# session whose turn the agent's max_tool_turns stopped) is restartable: a reopen needs a new human or
# trigger message, and the reopened invocation starts a fresh round count, so it cannot loop on its own.
_RESTARTABLE = {"completed", "failed", "cancelled", "tool_turn_cap"}


class OnlyAPersonReopens(ConflictError):
    """A message no person wrote was refused because the session ended as ``cancelled``.

    An operator's Cancel sticks (pivot P3 lead ruling R4, extended by lead ruling 1 of the G7 fixes): only a person's message reopens the session.
    That is a person's steer (the REST steer route, a channel reply), a person's Restart (the REST restart route), and a person's steer that was
    queued on the session and is realized by the drain after the Cancel (lead ruling of 2026-10-07 on ticket 01a1120e: the drain realizes on a
    cancelled end too). Every other message is refused: one from another session, a trigger fire, an agent's or an MCP client's steer or restart tool
    call, and any of those queued and realized later. ``cancelled`` is a restartable ended reason, so without this any of them would reopen the
    session the operator had just cancelled. It is raised under the session's lifecycle lock, on the row read there, before anything is written.
    """


def _refuse_a_cancelled_reopen(row: WorkspaceSession, *, session_id: str, by_a_person: bool) -> None:
    """Raise :class:`OnlyAPersonReopens` when ``row`` ended as ``cancelled`` and no person wrote the message that would reopen it."""
    if row.ended_reason == "cancelled" and not by_a_person:
        raise OnlyAPersonReopens(
            f"Session {session_id!r} was cancelled, and only a person's message or Restart reopens it"
        )


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
    by_a_person: bool,
    workspace_write: Callable[[], AbstractAsyncContextManager[None]] | None = None,
) -> tuple[WorkspaceSession, int]:
    """Reopen an ENDED session row IN PLACE — lock-free reopen body.

    Shared by :func:`reset_session` and ``wake_session``'s ENDED branch.
    Both callers already hold ``session_lifecycle_lock().acquire(session_id)``
    (a non-reentrant lock), so this helper must NOT re-acquire it — calling
    ``reset_session`` from inside ``wake_session``'s lock would deadlock.

    Assumes the caller verified ``row.status == SessionStatus.ENDED``.
    Refuses first a ``cancelled`` row when no person wrote the message that
    reopens it (``by_a_person=False``: :class:`OnlyAPersonReopens`, before
    anything is written), so the wake and the restart share one rule for it.
    Enforces the ``_RESTARTABLE`` ended_reason guard here (raising
    ConflictError for ``force_deleted`` always, and for ``workspace_lost``
    UNLESS the workspace has since healed - see below), reopens the
    on-disk slot, writes the ``INVOCATION_DIVIDER`` record, bumps the
    invocation counter, clears terminal + park bookkeeping (including
    ``ended_detail`` and the ``agent_phase*`` of the dead turn: the row is
    the one truth about how a session ended, so a stale detail must not
    label the NEXT end, and the phase belongs to the turn that is gone),
    and transitions
    the scheduler row ENDED -> CREATED (turn_status idle). Returns
    ``(reopened_row, invocation_number)``; does NOT publish events (the
    caller owns ticks). The divider is written BEFORE the caller appends any
    USER_INPUT so the invocation divider precedes the message.

    Recovery-aware ``workspace_lost`` (01a0533c, live SEV): a transient
    probe blip during a rollout can force-END every session in a workspace
    via :func:`primer.workspace.session_reconcile.reconcile_sessions_to_workspace_lost`
    well before the workspace actually stops serving, and the probe
    routinely recovers a few ticks later (``_HITS_BEFORE_RUNNING``) once
    the replacement pod is ready - at which point the sessions it just
    killed were never really lost, only unlucky. Blanket-treating
    ``workspace_lost`` as permanent (the pre-01a0533c behaviour) turns a
    30-90s infra blip into a PERMANENT dead end for the user: no message,
    steer, or reset can ever reach that session again, even though the
    workspace is healthy again by the time they try. So when
    ``ended_reason == "workspace_lost"``, re-check the CURRENT workspace
    row: if it still exists and reads ``phase == "running"``, the loss was
    transient and this reopen is allowed exactly like a normal
    completed/failed/cancelled restart. If the workspace is missing
    entirely or still ``phase != "running"``, the 409 stands - nothing to
    reopen into. ``force_deleted`` is a DIFFERENT, deliberate operator
    action (not a probe artifact) and stays permanent unconditionally.

    ``workspace_write`` (optional) is a factory of the context manager that wraps the writes INTO the workspace and nothing else: the slot reopen
    and the invocation divider. ``wake_session`` passes its guard there, the one that reports a failed write as "the workspace was removed". The
    calls this function makes to the database stay outside it: the ``workspace_lost`` recheck, the workspace lookup (which reads the workspace row
    and its template) and the closing row update. A refused pool connection is an ``OSError`` too, and under that guard it was reported as a
    removal of a workspace nobody had touched. Without the argument (``reset_session``) nothing wraps anything and every failure surfaces as it is.
    """
    _refuse_a_cancelled_reopen(row, session_id=session_id, by_a_person=by_a_person)
    if row.ended_reason not in _RESTARTABLE:
        recoverable = False
        if row.ended_reason == "workspace_lost":
            try:
                ws_row = await workspace_registry.get_workspace_row(workspace_id)
            except NotFoundError:
                ws_row = None
            recoverable = ws_row is not None and ws_row.phase == "running"
        if not recoverable:
            raise ConflictError(
                f"Session {session_id!r} ended as {row.ended_reason!r} and "
                "cannot be re-opened."
            )

    invocation = int(row.metadata.get("invocation", 1)) + 1

    # Resolving the workspace is a lookup in the database (its row, its template), not a write into the workspace: it stays outside the guard.
    ws = await workspace_registry.get_workspace(workspace_id)
    async with workspace_write() if workspace_write is not None else nullcontext():
        # 1. Reopen the on-disk slot (ENDED -> RUNNING; sanctioned exception).
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
        "turn_started_at": None,
        "last_turn_error": None,
        "ended_detail": None,
        "agent_phase": None,
        "agent_phase_turn_no": None,
        "agent_phase_stamped_at": None,
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
    human_intent: bool,
) -> tuple[WorkspaceSession, int]:
    """Re-open an ENDED session and append an invocation divider.

    Returns ``(row, invocation_number)``. Raises NotFoundError (missing /
    workspace mismatch) / ConflictError (not ENDED, or a non-restartable
    ``ended_reason`` such as ``workspace_lost``/``force_deleted``, or a
    ``cancelled`` session that no person is reopening: ``human_intent``,
    REQUIRED like :func:`~primer.session.enqueue.wake_session`'s, is True
    only for a person's Restart; :class:`OnlyAPersonReopens`).
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
            by_a_person=human_intent,
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
    human_intent: bool,
    reset_deps: "SessionResetDeps",
    wake_deps,  # primer.session.enqueue.SessionWakeDeps
    peer: PeerAttribution | None = None,
) -> WorkspaceSession:
    """Reset an ENDED session then invoke it (studio-agents-interact §5.3).

    Restart = reset-same-session (§5.2) + auto-wake (§5.1). Works for
    completed / failed / cancelled sessions; a non-ENDED (active) session
    is rejected by ``reset_session`` with a ConflictError (the restart 409
    gate). ``reset_session`` moves the row ENDED->CREATED first, so the
    subsequent ``wake_session`` sees CREATED and merely invokes — its ENDED
    reopen branch never fires here, so there is exactly one invocation
    divider (no double-reopen).

    ``human_intent`` (01a08c08) says whether a person stands behind the
    restart: True for the REST restart endpoint, False for the
    ``workspaces__restart_workspace_session`` tool (an agent, or an MCP
    client). ``reset_session`` refuses a ``cancelled`` session for a
    restart no person makes (:class:`OnlyAPersonReopens`: an operator's
    Cancel sticks). It is also forwarded to ``wake_session``, where it is a
    no-op for the pause in practice: ``_reopen_ended_locked`` already clears
    ``pause_requested`` as part of the ENDED->CREATED reopen (a stale pause
    from a PRIOR, now-ended invocation has nothing to do with THIS one).

    ``peer`` says the restart was sent by another session (the restart tool
    called from inside a workspace session, through
    :func:`primer.session.peer_messages.send_peer_message`): it is forwarded
    to ``wake_session``, so the USER_INPUT record names the sender
    (``payload["peer"]``) and the reopened turn's ``peer_hops`` is the
    message's hop count.
    """
    from primer.session.enqueue import wake_session

    await reset_session(
        workspace_id=workspace_id, session_id=session_id, deps=reset_deps, human_intent=human_intent,
    )
    return await wake_session(
        workspace_id=workspace_id,
        session_id=session_id,
        instruction=instruction,
        human_intent=human_intent,
        deps=wake_deps,
        peer=peer,
    )


__all__ = [
    "OnlyAPersonReopens",
    "SessionResetDeps",
    "_reopen_ended_locked",
    "reset_session",
    "restart_session",
]
