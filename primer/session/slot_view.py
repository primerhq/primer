"""The durable row is the one truth about how a session ended; the runtime slot is derived from it (architecture review A1).

A session has two records. The :class:`~primer.model.workspace_session.WorkspaceSession` ROW is what the scheduler, the claim
machinery and ``GET /v1/sessions/{sid}`` use. The SLOT is ``.state/sessions/<sid>/session.json`` in the workspace (the
:class:`~primer.model.workspace_session.SessionInfo` an ``AgentSession`` loads), which exists so the workspace and its tools can see
the session from inside. They are written by different code on different processes, and the slot is demonstrably wrong in three
ways:

* it says ``completed`` for a row that ended some other way. The dispatch mirror (``_sync_agent_session_ended``) writes
  ``completed`` for any reason outside the four an ``AgentSession`` accepts, and a closed workspace handle
  (``Workspace.aclose``) USED TO end every cached slot as ``completed`` whatever the row says (fixed in A-24: it only releases the handle
  now, but slots it ended before that fix are still on disk, which is why this overlay stays);
* it still says ``running`` or ``waiting`` for a row that was ended by a path that never touches the slot (the stuck-session
  sweeper, the pool's ``_end_session``, the reconciler);
* it says ``ended`` for a row that is alive (a handle closed under a parked session).

Every reader that serves the slot to a person or a tool therefore passes it through :func:`overlay_row_on_info` (one session) or
:func:`overlay_rows_on_infos` (a page) first, so two routes can never tell two stories. The overlay owns the LIFECYCLE fields only
(``status``, ``ended_reason``, ``ended_detail``, ``ended_at``) and ``last_turn_error`` (the row's alone); everything else in the slot (name, agent, timestamps) is served as it
is. It corrects exactly three disagreements: a row that is ENDED (the row's end, whatever the slot says), a slot that says ENDED under a
row that is not, and a slot still RUNNING under a WAITING row. Every OTHER disagreement between two live states (a slot WAITING or
PAUSED under a RUNNING row, a slot RUNNING under a CREATED row) is left alone on purpose: those are in-turn states the executor writes to
the slot first, nothing shows the row is more current there, and the claim here is about how a session ENDED, not a general merge. A slot with no row, or whose row belongs to another workspace, is served unchanged: there is nothing to derive it from.

This does not rewrite ``session.json``. Readers that DECIDE something from the slot (``WorkspaceAgentExecutor.invoke``, a wake's
``append_instruction``) still read it directly. The write side (A-24) stopped one cause of a wrong slot, a handle close ending live
slots; the others in the list above (the sweeper, the pool's ``_end_session``, the dispatch mirror's fallback) still leave the file
behind the row.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from typing import Any

from primer.model.storage import OffsetPage
from primer.model.workspace_session import SessionInfo, SessionStatus, WorkspaceSession
from primer.storage._predicate import FieldRef, Op, Predicate, Value

logger = logging.getLogger(__name__)

# The most rows one storage query can return (OffsetPage.length is capped at 200); a longer list is read in chunks.
_MAX_PAGE = 200


def overlay_row_on_info(
    info: SessionInfo, row: WorkspaceSession | None, *, workspace_id: str | None = None,
) -> SessionInfo:
    """Return ``info`` with its lifecycle fields taken from ``row``, the one truth.

    * ``row`` ENDED: the session ended, with the row's reason, detail and time (the slot's reason is kept only if the row has none).
    * the slot says ENDED but the row does not: the session is alive, so it has no end reason, detail or time.
    * the slot still RUNNING under a WAITING row: a clean rest, which the row records and the slot never learns about.

    ``workspace_id`` (when given) must match the row's: a row for the same id on another workspace is not this session's.
    """
    if row is None or (workspace_id is not None and row.workspace_id != workspace_id):
        return info
    # The failure of the last turn is the row's too (the slot never carries it): served with every lifecycle answer below, so a session that
    # RESTS after a failed turn (WAITING, no ended_reason) still says so to a tool that reads the slot.
    failure = {"last_turn_error": row.last_turn_error}
    if row.status == SessionStatus.ENDED:
        return info.model_copy(update={
            "status": SessionStatus.ENDED,
            "ended_reason": row.ended_reason if row.ended_reason is not None else info.ended_reason,
            "ended_detail": row.ended_detail,
            "ended_at": row.ended_at if row.ended_at is not None else info.ended_at,
            **failure,
        })
    if info.status == SessionStatus.ENDED:
        return info.model_copy(update={
            "status": row.status, "ended_reason": None, "ended_detail": None, "ended_at": None, **failure,
        })
    if info.status == SessionStatus.RUNNING and row.status == SessionStatus.WAITING:
        return info.model_copy(update={"status": SessionStatus.WAITING, **failure})
    return info.model_copy(update=failure)


async def overlay_row_on_slot_info(
    info: SessionInfo, session_storage: Any, *, workspace_id: str | None = None,
) -> SessionInfo:
    """:func:`overlay_row_on_info` for one session, reading its row. A storage failure serves the slot as it is (and says so)."""
    try:
        row = await session_storage.get(info.session_id)
    except Exception:  # noqa: BLE001 -- advisory: the slot is still an answer
        logger.warning("session slot view: reading the row of %s failed; serving the slot as it is", info.session_id, exc_info=True)
        return info
    return overlay_row_on_info(info, row, workspace_id=workspace_id)


async def overlay_rows_on_infos(
    infos: Sequence[SessionInfo], session_storage: Any, *, workspace_id: str | None = None,
) -> list[SessionInfo]:
    """:func:`overlay_row_on_info` for a page of sessions: one query per 200 sessions, never one per session."""
    if not infos:
        return []
    ids = _unique(i.session_id for i in infos)
    rows: dict[str, WorkspaceSession] = {}
    try:
        for start in range(0, len(ids), _MAX_PAGE):
            chunk = ids[start : start + _MAX_PAGE]
            page = await session_storage.find(
                Predicate(left=FieldRef(name="id"), op=Op.IN, right=Value(value=chunk)),
                OffsetPage(offset=0, length=len(chunk)),
            )
            rows.update((r.id, r) for r in page.items)
    except Exception:  # noqa: BLE001 -- advisory: the slots are still an answer
        logger.warning("session slot view: reading the rows of %d sessions failed; serving the slots as they are", len(ids), exc_info=True)
        return list(infos)
    return [overlay_row_on_info(i, rows.get(i.session_id), workspace_id=workspace_id) for i in infos]


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


__all__ = ["overlay_row_on_info", "overlay_row_on_slot_info", "overlay_rows_on_infos"]
