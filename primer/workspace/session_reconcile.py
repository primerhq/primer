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

from primer.model.storage import CursorPage, FieldRef, Op, Predicate, Value
from primer.model.workspace_session import SessionStatus, WorkspaceSession

if TYPE_CHECKING:
    from primer.int.storage_provider import StorageProvider


logger = logging.getLogger(__name__)

_LIST_PAGE_SIZE = 200

# A session is open while it is anything but ENDED. Spelled as "not ended" so a status added later is open by default (the safe direction here).
# It is part of the query, not a filter on the page: ENDED rows must not use up the pages that hold the open ones.
_NOT_ENDED = Predicate(left=FieldRef(name="status"), op=Op.NE, right=Value(value=SessionStatus.ENDED.value))


async def reconcile_sessions_to_workspace_lost(
    sp: "StorageProvider", workspace_id: str,
) -> int:
    """Mark every non-ENDED session on *workspace_id* ENDED/``workspace_lost``.

    Best-effort: storage/query/update failures are logged and swallowed
    rather than raised, since callers must not fail their own operation
    (a probe tick, a workspace destroy) because one session row couldn't
    be updated. Returns the number of sessions reconciled.

    Every open session is read before any is changed (the ticket 01a11b93 bug: one page of 200 rows, ENDED ones included, so a workspace with more
    sessions than that kept its open ones past page 1 running against a workspace the destroy was about to delete). The read pages by cursor
    over "this workspace AND not ended"; nothing is written while it pages, so ending a row cannot move the cursor under it. If a later page
    cannot be read, the sessions already read are still reconciled and the failure is logged.
    """
    try:
        session_storage = sp.get_storage(WorkspaceSession)
    except Exception:  # noqa: BLE001 -- storage layer unavailable
        logger.warning(
            "session reconcile: storage unavailable, cannot reconcile %s",
            workspace_id,
        )
        return 0

    match = Predicate(
        left=Predicate(left=FieldRef(name="workspace_id"), op=Op.EQ, right=Value(value=workspace_id)),
        op=Op.AND,
        right=_NOT_ENDED,
    )
    open_sessions: list[WorkspaceSession] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    try:
        while True:
            page = await session_storage.find(match, CursorPage(cursor=cursor, length=_LIST_PAGE_SIZE))
            open_sessions.extend(page.items)
            # A backend that hands back a cursor it has already given would loop forever.
            if page.next_cursor is None or page.next_cursor in seen_cursors:
                break
            seen_cursors.add(page.next_cursor)
            cursor = page.next_cursor
    except Exception:  # noqa: BLE001 -- find unavailable
        logger.exception(
            "session reconcile: failed to query sessions on %s (reconciling the %d already read)",
            workspace_id, len(open_sessions),
        )

    now = datetime.now(timezone.utc)
    reconciled = 0
    for sess in open_sessions:
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
