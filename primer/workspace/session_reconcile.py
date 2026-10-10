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

from pydantic_core import to_jsonable_python

from primer.model.except_ import NotFoundError
from primer.model.storage import CursorPage, FieldRef, Op, Predicate, Value
from primer.model.workspace_session import NON_ENDED_STATUSES, SessionStatus, WorkspaceSession

if TYPE_CHECKING:
    from primer.int.storage_provider import StorageProvider


logger = logging.getLogger(__name__)

_LIST_PAGE_SIZE = 200

# A session is open while it is anything but ENDED. Spelled as "not ended" so a status added later is open by default (the safe direction here).
# It is part of the query, not a filter on the page: ENDED rows must not use up the pages that hold the open ones.
_NOT_ENDED = Predicate(left=FieldRef(name="status"), op=Op.NE, right=Value(value=SessionStatus.ENDED.value))


async def _say_why_refused(session_storage, session_id: str, workspace_id: str) -> None:
    """Log why the fenced patch of ``session_id`` did not apply, from a fresh read of the row.

    ENDED: another path ended it (the fence did its job), INFO. Still open: the fence refused a row it should have matched, and the session stays open on a workspace that is gone,
    WARNING naming the session. Deleted since: WARNING. A read that fails is logged and goes no further: the other sessions are still ended.
    """
    try:
        row = await session_storage.get(session_id)
    except Exception:  # noqa: BLE001 -- log + continue
        logger.exception(
            "session reconcile: session %s on %s was not reconciled (its fenced patch was refused) and the row could not be read again to say why",
            session_id, workspace_id,
        )
        return
    if row is None:
        logger.warning("session reconcile: session %s on %s was deleted before it could be reconciled", session_id, workspace_id)
    elif row.status == SessionStatus.ENDED:
        logger.info(
            "session reconcile: session %s on %s left alone: ended by another path (ENDED/%s)", session_id, workspace_id, row.ended_reason,
        )
    else:
        logger.warning(
            "session reconcile: session %s on %s is still open (%s) although its fenced patch was refused; it stays open on a workspace that is gone",
            session_id, workspace_id, getattr(row.status, "value", row.status),
        )


async def reconcile_sessions_to_workspace_lost(
    sp: "StorageProvider", workspace_id: str,
) -> int:
    """Mark every non-ENDED session on *workspace_id* ENDED/``workspace_lost``.

    Best-effort: storage/query/update failures are logged and swallowed
    rather than raised, since callers must not fail their own operation
    (a probe tick, a workspace destroy) because one session row couldn't
    be updated. Returns the number of sessions reconciled.

    Each session is ended by ONE field-scoped, fenced write (``patch_if`` of the eight fields below, ``where status`` is not ended), never a whole-document
    write of the snapshot read at the start (ticket 01a11d29). A session another path ended since the read (a turn's own end, a force delete's closure, the
    preempt convergence) keeps that path's reason and is not counted; a field another writer committed since (a steer's ``last_seq``) is not put back; a
    session deleted since is skipped with a warning.

    A refused patch is never silent: the row is read again, and it is logged at INFO as left alone when it is ENDED (another path ended it), at WARNING naming the
    session when it is still open (a fence that refused a row it should have matched leaves a session running on a workspace that is gone), and at WARNING or
    ERROR when the row cannot be read. None of them is counted: the return value is the number of sessions THIS call ended.

    Every open session is read before any is changed (the ticket 01a11b93 bug: one page of 200 rows, ENDED ones included, so a workspace with more
    sessions than that kept its open ones past page 1 running against a workspace whose runtime the destroy had just torn down). The read pages by
    cursor over "this workspace AND not ended"; nothing is written while it pages, so ending a row cannot move the cursor under it. If a later
    page cannot be read, the sessions already read are still reconciled and the failure is logged.

    A read that fails is not a reason for a destroy to refuse or retry (decided under ticket 01a11b93): the backend is already torn down when this
    runs, so refusing would leave a row without a runtime, a retry inside the request cannot make a failing query work, and the probe cannot rescue
    the sessions later because the row is gone. The failure is logged ("failed to query sessions"), the sessions already read are ended, and the
    destroy goes on; the sessions a failed read leaves open are the cost.
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
    # The fields this writer owns, and nothing else: the snapshot it read can be stale by the time it writes (a steer's last_seq, a cancel flag), and a
    # whole-document write of it would put all of that back.
    patch = to_jsonable_python({
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
        # The agent phase belongs to the turn that is gone with the workspace (the model says it is None whenever turn_status is idle), and the same
        # crashed-turn cleanup clears it with turn_started_at.
        "agent_phase": None,
        "agent_phase_turn_no": None,
        "agent_phase_stamped_at": None,
    })
    reconciled = 0
    for sess in open_sessions:
        if sess.status == SessionStatus.ENDED:
            continue
        try:
            # Fenced on "not ended" in the statement itself: a turn's own end, a force delete or the preempt convergence that ended the row since the read
            # keeps its reason ("the first terminal reason wins"), and is not counted as reconciled here.
            written = await session_storage.patch_if(sess.id, patch, where={"status": NON_ENDED_STATUSES()})
        except NotFoundError:
            logger.warning("session reconcile: session %s on %s was deleted before it could be reconciled", sess.id, workspace_id)
            continue
        except Exception:  # noqa: BLE001 -- log + continue
            logger.exception(
                "session reconcile: failed to reconcile session %s on %s",
                sess.id, workspace_id,
            )
            continue
        if written is not None:
            reconciled += 1
        else:
            await _say_why_refused(session_storage, sess.id, workspace_id)

    if reconciled:
        logger.info(
            "session reconcile: reconciled %d session(s) on %s as "
            "ENDED/workspace_lost",
            reconciled, workspace_id,
        )
    return reconciled


__all__ = ["reconcile_sessions_to_workspace_lost"]
