"""Shared workspace-session reconciliation to ENDED/``workspace_lost``.

Used whenever a workspace becomes permanently unreachable and every
non-ENDED session still pointing at it needs to be closed out, or the
row is orphaned forever (no worker can ever re-attach to a runtime
that's gone). Two callers today: the health probe (three-strike ping
failure -> ``phase="failed"``, see :mod:`primer.workspace.probe`) and
:meth:`primer.api.registries.workspace_registry.WorkspaceRegistry.destroy`
(the workspace row is about to be deleted outright, so there is no
later probe transition for it to ride on).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from primer.model.storage import FieldRef, OffsetPage, Op, Predicate, Value
from primer.model.workspace_session import SessionStatus, WorkspaceSession

if TYPE_CHECKING:
    from primer.int.storage_provider import StorageProvider


logger = logging.getLogger(__name__)

_LIST_PAGE_SIZE = 200


async def reconcile_sessions_to_workspace_lost(
    sp: "StorageProvider", workspace_id: str,
) -> int:
    """Mark every non-ENDED session on *workspace_id* ENDED/``workspace_lost``.

    Best-effort: storage/query/update failures are logged and swallowed
    rather than raised, since callers must not fail their own operation
    (a probe tick, a workspace destroy) because one session row couldn't
    be updated. Returns the number of sessions reconciled.
    """
    try:
        session_storage = sp.get_storage(WorkspaceSession)
    except Exception:  # noqa: BLE001 -- storage layer unavailable
        logger.warning(
            "session reconcile: storage unavailable, cannot reconcile %s",
            workspace_id,
        )
        return 0

    try:
        page = await session_storage.find(
            Predicate(
                left=FieldRef(name="workspace_id"),
                op=Op.EQ,
                right=Value(value=workspace_id),
            ),
            OffsetPage(offset=0, length=_LIST_PAGE_SIZE),
        )
    except Exception:  # noqa: BLE001 -- find unavailable
        logger.exception(
            "session reconcile: failed to query sessions on %s", workspace_id,
        )
        return 0

    now = datetime.now(timezone.utc)
    reconciled = 0
    for sess in page.items:
        if sess.status == SessionStatus.ENDED:
            continue
        updated_sess = sess.model_copy(update={
            "status": SessionStatus.ENDED,
            "ended_reason": "workspace_lost",
            "ended_at": now,
            # A worker whose workspace just went permanently unreachable is
            # exactly the crash scenario turn_started_at exists to catch: it
            # never reached run_one_session_turn's own cleanup (finally /
            # build-executor-failure paths), so turn_status could still read
            # "running" here. The workspace being gone forever makes any
            # value moot - reset unconditionally rather than gating on the
            # current value like the finally-block clear does.
            "turn_status": "idle",
            "turn_started_at": None,
        })
        try:
            await session_storage.update(updated_sess)
        except Exception:  # noqa: BLE001 -- log + continue
            logger.exception(
                "session reconcile: failed to reconcile session %s on %s",
                sess.id, workspace_id,
            )
            continue
        reconciled += 1

    if reconciled:
        logger.info(
            "session reconcile: reconciled %d session(s) on %s as "
            "ENDED/workspace_lost",
            reconciled, workspace_id,
        )
    return reconciled


__all__ = ["reconcile_sessions_to_workspace_lost"]
