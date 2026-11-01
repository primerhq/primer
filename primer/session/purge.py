"""Deleting a session for good: the one cleanup the DELETE route, a workspace destroy and the retention sweep share (GR-6, ruling D16).

A session's row is not the only thing it leaves: the slot ``.state/sessions/<sid>/`` in its workspace, the thread mappings that point at it, the
ledger rows of the peer messages it sent, and its canvas. Each cleanup but the row delete is best effort (a failing one is logged and the row is
still removed: a leftover holds no session and serves nothing). ``ToolApprovalRecord`` rows are left alone (they are the audit trail).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from primer.model.except_ import NotFoundError
from primer.model.storage import FieldRef, OffsetPage, Op, OrderBy, Predicate, Value
from primer.model.workspace_session import SessionStatus, WorkspaceSession

logger = logging.getLogger(__name__)

_PAGE = 100


async def delete_session_rows(
    storage_provider: Any, session_id: str, *, workspace_registry: Any = None, workspace_id: str | None = None,
) -> None:
    """Remove ``session_id`` and everything that hangs off it.

    With a ``workspace_registry`` and a ``workspace_id`` the session's slot in its (live) workspace is reaped too; a workspace that is gone has no slot.
    The caller has already ended the session and resolved its pending external tool calls.
    """
    if workspace_registry is not None and workspace_id is not None:
        # Each backend reaps its OWN slot inside remove_session (LocalWorkspace rmtrees .state/sessions/<sid>/; SandboxWorkspace git-rm's the slot in the
        # pod's runtime state repo), so the persisted slot cannot rehydrate the deleted session on the next list.
        try:
            live_workspace = await workspace_registry.get_workspace(workspace_id)
            await live_workspace.remove_session(session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "delete_session: session-slot cleanup failed (row still removed)",
                extra={"session_id": session_id, "workspace_id": workspace_id, "exception": type(exc).__name__, "error": str(exc)},
            )
    # The thread mappings that pointed here: a leaked mapping would steer a session that no longer exists (S6 section 9).
    try:
        from primer.channel.correlation import CorrelationStore

        await CorrelationStore(storage_provider).clear_for_session(session_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("delete_session: correlation cleanup failed (row still removed)", extra={"session_id": session_id, "error": str(exc)})
    # The peer-message ledger rows of the messages this session sent are its rate allowance, which nothing will ask about again (audit G7-15).
    try:
        from primer.session.peer_messages import delete_peer_ledger_of

        await delete_peer_ledger_of(storage_provider, session_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("delete_session: peer-ledger cleanup failed (row still removed)", extra={"session_id": session_id, "error": str(exc)})
    # The session's canvas (one SessionCanvas row per session, id = session id). A session that never drew has no row.
    try:
        from primer.model.canvas import SessionCanvas

        await storage_provider.get_storage(SessionCanvas).delete(session_id)
    except NotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("delete_session: canvas cleanup failed (row still removed)", extra={"session_id": session_id, "error": str(exc)})
    try:
        await storage_provider.get_storage(WorkspaceSession).delete(session_id)
    except NotFoundError:
        pass            # already gone: a retried or racing delete is done


async def _resolve_external_calls(storage_provider: Any, session_id: str, reason: str) -> None:
    from primer.model.external_tool import ExternalToolCall
    from primer.session.external_tools import cancel_pending_external

    try:
        await cancel_pending_external(call_storage=storage_provider.get_storage(ExternalToolCall), session_id=session_id, reason=reason)
    except Exception as exc:  # noqa: BLE001
        logger.warning("delete_session: pending external calls not resolved", extra={"session_id": session_id, "error": str(exc)})


async def purge_workspace_sessions(storage_provider: Any, workspace_id: str) -> int:
    """Delete every session of a workspace that is being destroyed (they are ended by then); the number deleted.

    Without this they are orphans: ``require_session`` answers a dangling workspace id with 404, so their owner can never delete them. The workspace
    and its slots are already gone, so no slot is reaped.
    """
    storage = storage_provider.get_storage(WorkspaceSession)
    predicate = Predicate(left=FieldRef(name="workspace_id"), op=Op.EQ, right=Value(value=workspace_id))
    deleted = 0
    while True:
        page = await storage.find(predicate, OffsetPage(offset=0, length=_PAGE, want_total=False))
        if not page.items:
            return deleted
        for row in page.items:
            await _resolve_external_calls(storage_provider, row.id, "workspace destroyed")
            await delete_session_rows(storage_provider, row.id)
            deleted += 1
        if len(page.items) < _PAGE:
            return deleted


async def sweep_ended_sessions(
    storage_provider: Any, *, retention_days: int | None, batch: int = _PAGE, workspace_registry: Any = None, now: datetime | None = None,
) -> int:
    """Delete up to ``batch`` ENDED sessions whose ``ended_at`` is older than ``retention_days``; the number deleted. ``None`` (the default) deletes nothing.

    Read oldest first, so a page whose first row is inside the window ends the scan; a row with no ``ended_at`` is never eligible and is skipped.
    """
    if retention_days is None:
        return 0
    cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
    storage = storage_provider.get_storage(WorkspaceSession)
    predicate = Predicate(left=FieldRef(name="status"), op=Op.EQ, right=Value(value=SessionStatus.ENDED.value))
    deleted, offset = 0, 0
    while deleted < batch:
        page = await storage.find(
            predicate, OffsetPage(offset=offset, length=_PAGE, want_total=False), order_by=[OrderBy(field="ended_at", direction="asc")],
        )
        if not page.items:
            break
        skipped = 0
        for row in page.items:
            if row.ended_at is None:
                skipped += 1
                continue
            if row.ended_at >= cutoff:
                return deleted
            await _resolve_external_calls(storage_provider, row.id, "session expired")
            await delete_session_rows(storage_provider, row.id, workspace_registry=workspace_registry, workspace_id=row.workspace_id)
            deleted += 1
            if deleted >= batch:
                return deleted
        if len(page.items) < _PAGE:
            break
        offset = skipped            # what was deleted shifts the next page up; only the rows kept (no ended_at) stay in front of it
    return deleted


__all__ = ["delete_session_rows", "purge_workspace_sessions", "sweep_ended_sessions"]
