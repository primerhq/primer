"""The rule that derives a session's runtime view from its durable row, and its one query per page (architecture review A1).

``tests/api/test_session_end_one_truth.py`` pins it through the routes and the tools; this file pins the function itself: every
combination of what the slot and the row can say, what it leaves alone, and how it reads rows.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from primer.model.workspace_session import AgentSessionBinding, SessionInfo, SessionStatus, WorkspaceSession
from primer.session.slot_view import overlay_row_on_info, overlay_row_on_slot_info, overlay_rows_on_infos

NOW = datetime(2026, 10, 8, 9, 0, tzinfo=timezone.utc)
ROW_ENDED_AT = datetime(2026, 10, 8, 10, 0, tzinfo=timezone.utc)
LIVE = [SessionStatus.CREATED, SessionStatus.RUNNING, SessionStatus.WAITING, SessionStatus.PAUSED]


def _slot(sid: str = "s1", status=SessionStatus.RUNNING, **fields) -> SessionInfo:
    return SessionInfo(
        session_id=sid, agent_id="ag-slot", workspace_id="ws-1", name="friendly", status=status,
        started_at=NOW, last_activity_at=NOW, **fields,
    )


def _row(sid: str = "s1", status=SessionStatus.ENDED, workspace_id: str = "ws-1", **fields) -> WorkspaceSession:
    return WorkspaceSession(
        id=sid, workspace_id=workspace_id, binding=AgentSessionBinding(agent_id="ag-row"), status=status, created_at=NOW, **fields,
    )


def test_no_row_means_the_slot_is_served_as_it_is():
    slot = _slot(status=SessionStatus.ENDED, ended_reason="cancelled", ended_at=NOW)

    assert overlay_row_on_info(slot, None) == slot


def test_a_row_of_another_workspace_is_not_this_sessions_row():
    slot = _slot()
    row = _row(workspace_id="ws-other", ended_reason="failed", ended_at=ROW_ENDED_AT)

    assert overlay_row_on_info(slot, row, workspace_id="ws-1") == slot


@pytest.mark.parametrize("slot_status", [SessionStatus.RUNNING, SessionStatus.WAITING, SessionStatus.ENDED])
def test_an_ended_row_gives_the_reason_the_detail_and_the_time(slot_status):
    slot = _slot(status=slot_status, ended_reason="completed" if slot_status == SessionStatus.ENDED else None)
    row = _row(ended_reason="failed", ended_detail="never_started", ended_at=ROW_ENDED_AT)

    got = overlay_row_on_info(slot, row, workspace_id="ws-1")

    assert (got.status, got.ended_reason, got.ended_detail, got.ended_at) == (
        SessionStatus.ENDED, "failed", "never_started", ROW_ENDED_AT,
    )


def test_an_ended_row_with_no_reason_or_time_keeps_what_the_slot_knows():
    slot = _slot(status=SessionStatus.ENDED, ended_reason="cancelled", ended_at=NOW)

    got = overlay_row_on_info(slot, _row(), workspace_id="ws-1")

    assert (got.ended_reason, got.ended_at, got.ended_detail) == ("cancelled", NOW, None)


@pytest.mark.parametrize("row_status", LIVE)
def test_a_live_row_unends_a_slot_that_says_ended(row_status):
    slot = _slot(status=SessionStatus.ENDED, ended_reason="completed", ended_at=NOW)

    got = overlay_row_on_info(slot, _row(status=row_status), workspace_id="ws-1")

    assert (got.status, got.ended_reason, got.ended_detail, got.ended_at) == (row_status, None, None, None)


def test_a_clean_rest_shows_through_a_slot_that_still_says_running():
    got = overlay_row_on_info(_slot(status=SessionStatus.RUNNING), _row(status=SessionStatus.WAITING), workspace_id="ws-1")

    assert got.status == SessionStatus.WAITING


@pytest.mark.parametrize(
    ("slot_status", "row_status"),
    [
        (SessionStatus.WAITING, SessionStatus.RUNNING),
        (SessionStatus.PAUSED, SessionStatus.RUNNING),
        (SessionStatus.RUNNING, SessionStatus.RUNNING),
        (SessionStatus.RUNNING, SessionStatus.CREATED),
    ],
)
def test_the_other_live_combinations_are_left_alone(slot_status, row_status):
    slot = _slot(status=slot_status)

    assert overlay_row_on_info(slot, _row(status=row_status), workspace_id="ws-1") == slot


def test_only_the_lifecycle_fields_are_derived():
    slot = _slot(status=SessionStatus.RUNNING)
    row = _row(ended_reason="failed", ended_detail="never_started", ended_at=ROW_ENDED_AT)

    got = overlay_row_on_info(slot, row, workspace_id="ws-1")

    assert (got.name, got.agent_id, got.session_id, got.started_at, got.last_activity_at) == (
        "friendly", "ag-slot", "s1", NOW, NOW,
    )


# ---- reading the rows ---------------------------------------------------------------------------------------------------


class _Storage:
    def __init__(self, rows: list[WorkspaceSession], *, fail: bool = False) -> None:
        self._rows = {r.id: r for r in rows}
        self.fail = fail
        self.finds: list[list[str]] = []
        self.gets: list[str] = []

    async def find(self, predicate, page):
        if self.fail:
            raise RuntimeError("storage is down")
        ids = list(predicate.right.value)
        self.finds.append(ids)
        assert page.length == len(ids) <= 200
        return SimpleNamespace(items=[self._rows[i] for i in ids if i in self._rows])

    async def get(self, session_id):
        if self.fail:
            raise RuntimeError("storage is down")
        self.gets.append(session_id)
        return self._rows.get(session_id)


@pytest.mark.asyncio
async def test_a_page_is_one_query_not_one_per_session():
    slots = [_slot(f"s{n}") for n in range(25)]
    storage = _Storage([_row(f"s{n}", ended_reason="failed", ended_detail="never_started", ended_at=ROW_ENDED_AT) for n in range(25)])

    got = await overlay_rows_on_infos(slots, storage, workspace_id="ws-1")

    assert len(storage.finds) == 1 and storage.gets == []
    assert [g.ended_detail for g in got] == ["never_started"] * 25 and [g.session_id for g in got] == [s.session_id for s in slots]


@pytest.mark.asyncio
async def test_more_than_a_storage_page_is_read_in_chunks_and_every_session_is_covered():
    slots = [_slot(f"s{n}") for n in range(450)]
    storage = _Storage([_row(f"s{n}", ended_reason="failed", ended_at=ROW_ENDED_AT) for n in range(450)])

    got = await overlay_rows_on_infos(slots, storage, workspace_id="ws-1")

    assert [len(f) for f in storage.finds] == [200, 200, 50]
    assert all(g.status == SessionStatus.ENDED for g in got), "a session past the first chunk must not be left on its slot's word"


@pytest.mark.asyncio
async def test_a_session_with_no_row_in_the_page_keeps_its_slot_and_the_order_is_kept():
    slots = [_slot("s1"), _slot("s2"), _slot("s3")]
    storage = _Storage([_row("s2", ended_reason="failed", ended_at=ROW_ENDED_AT)])

    got = await overlay_rows_on_infos(slots, storage, workspace_id="ws-1")

    assert [(g.session_id, g.status) for g in got] == [
        ("s1", SessionStatus.RUNNING), ("s2", SessionStatus.ENDED), ("s3", SessionStatus.RUNNING),
    ]


@pytest.mark.asyncio
async def test_an_empty_page_asks_nothing():
    storage = _Storage([])

    assert await overlay_rows_on_infos([], storage) == []
    assert storage.finds == []


@pytest.mark.asyncio
async def test_a_storage_failure_serves_the_slots_as_they_are_and_says_so(caplog):
    slots = [_slot("s1"), _slot("s2")]

    with caplog.at_level(logging.WARNING, logger="primer.session.slot_view"):
        got_page = await overlay_rows_on_infos(slots, _Storage([], fail=True))
        got_one = await overlay_row_on_slot_info(slots[0], _Storage([], fail=True), workspace_id="ws-1")

    assert got_page == slots and got_one == slots[0]
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2, "an answer from the slot alone must be logged"


@pytest.mark.asyncio
async def test_one_session_reads_its_row_with_a_get():
    storage = _Storage([_row("s1", ended_reason="failed", ended_detail="never_started", ended_at=ROW_ENDED_AT)])

    got = await overlay_row_on_slot_info(_slot("s1"), storage, workspace_id="ws-1")

    assert storage.gets == ["s1"] and storage.finds == []
    assert (got.status, got.ended_reason, got.ended_detail) == (SessionStatus.ENDED, "failed", "never_started")


@pytest.mark.parametrize("slot_status, row_status", [
    (SessionStatus.RUNNING, SessionStatus.WAITING),     # a session that RESTS after a failed turn: the slot never learns it rested
    (SessionStatus.ENDED, SessionStatus.WAITING),       # ... and the executor ended the slot when the turn failed
    (SessionStatus.WAITING, SessionStatus.WAITING),
    (SessionStatus.RUNNING, SessionStatus.ENDED),
])
def test_the_failure_of_the_last_turn_is_the_rows_alone_and_is_served_with_every_answer(slot_status, row_status):
    """C-024: a session that rests after a transport failure has no ``ended_reason``; ``last_turn_error`` is how the MCP workspace tools, which
    read the slot, see that its last turn failed."""
    from primer.model.workspace_session import LastTurnError

    failure = LastTurnError(code="server_error", at=ROW_ENDED_AT)
    row = _row(status=row_status, last_turn_error=failure, **({"ended_reason": "failed"} if row_status == SessionStatus.ENDED else {}))

    got = overlay_row_on_info(_slot(status=slot_status), row, workspace_id="ws-1")

    assert got.last_turn_error == failure
    assert overlay_row_on_info(_slot(status=slot_status), _row(status=row_status), workspace_id="ws-1").last_turn_error is None
