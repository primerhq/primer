"""WorkspaceSession's park-batch marker and wake stamp, and the derived status lists (plan 3.3 and 3.4 b-0).

``parked_tool_batches`` is a selector-only marker: ``True`` while the park references a tool_wait batch, cleared by
writing ``None`` (JSON null) and never ``False``, because a ``False`` would sit under the ``IS NOT NULL`` partial
index and index every cleared row. ``resumable_at`` is the wake's own timestamp. Nothing writes either yet, so both
are served as null on every session read.

``NON_ENDED_STATUSES()`` / ``NON_ENDED_STATUSES_NOT_PAUSED()`` are computed from ``SessionStatus`` at call time, so a
status added later is included without anyone editing a list.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum

import pytest
from pydantic import ValidationError

import primer.model.workspace_session as ws_model
from primer.model.common import dump_for_storage
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession

def _session(**kw) -> WorkspaceSession:
    return WorkspaceSession(
        id="s1", workspace_id="w1", binding=AgentSessionBinding(agent_id="a1"),
        status=SessionStatus.RUNNING, created_at=datetime.now(timezone.utc), **kw,
    )


# ---------------------------------------------------------------------------
# parked_tool_batches and resumable_at
# ---------------------------------------------------------------------------


def test_both_fields_default_to_null_and_are_served() -> None:
    s = _session()

    assert s.parked_tool_batches is None
    assert s.resumable_at is None
    served = json.loads(s.model_dump_json())
    assert "parked_tool_batches" in served and served["parked_tool_batches"] is None
    assert "resumable_at" in served and served["resumable_at"] is None


def test_a_marked_and_stamped_row_round_trips_through_json_and_storage() -> None:
    stamp = datetime(2026, 10, 5, 12, 30, tzinfo=timezone.utc)
    s = _session(parked_tool_batches=True, resumable_at=stamp)

    back = WorkspaceSession.model_validate_json(s.model_dump_json())
    stored = dump_for_storage(s)

    assert back.parked_tool_batches is True
    assert back.resumable_at == stamp
    assert stored["parked_tool_batches"] is True
    assert WorkspaceSession.model_validate(stored).resumable_at == stamp


def test_the_marker_is_cleared_to_null_and_false_is_refused() -> None:
    """``Literal[True] | None``: the only cleared value is ``None``; ``False`` would land in the partial index."""
    cleared = _session(parked_tool_batches=True).model_copy(update={"parked_tool_batches": None})

    assert dump_for_storage(cleared)["parked_tool_batches"] is None
    with pytest.raises(ValidationError):
        _session(parked_tool_batches=False)
    with pytest.raises(ValidationError):
        WorkspaceSession.model_validate({**dump_for_storage(_session()), "parked_tool_batches": False})


def test_a_row_written_before_the_fields_existed_reads_them_as_null() -> None:
    stored = dump_for_storage(_session())
    del stored["parked_tool_batches"], stored["resumable_at"]

    row = WorkspaceSession.model_validate(stored)

    assert row.parked_tool_batches is None and row.resumable_at is None


# ---------------------------------------------------------------------------
# The derived status lists
# ---------------------------------------------------------------------------


def test_non_ended_statuses_is_every_status_but_ended() -> None:
    statuses = ws_model.NON_ENDED_STATUSES()

    assert "ended" not in statuses
    assert set(statuses) | {"ended"} == {s.value for s in SessionStatus}
    assert len(statuses) == len(set(statuses))
    assert all(type(s) is str for s in statuses)


def test_non_ended_statuses_not_paused_is_every_status_but_ended_and_paused() -> None:
    statuses = ws_model.NON_ENDED_STATUSES_NOT_PAUSED()

    assert "ended" not in statuses and "paused" not in statuses
    assert set(statuses) | {"ended", "paused"} == {s.value for s in SessionStatus}
    assert len(statuses) == len(set(statuses))
    assert all(type(s) is str for s in statuses)


def test_both_lists_are_derived_at_call_time_so_a_new_status_is_included(monkeypatch) -> None:
    """A status added to the enum joins both lists with no edit: a hand-written list would leave it out, and a hook
    whose ``where`` named that list would silently refuse to wake a session in the new status."""

    class _GrownStatus(str, Enum):
        CREATED = "created"
        RUNNING = "running"
        WAITING = "waiting"
        PAUSED = "paused"
        ENDED = "ended"
        ARCHIVING = "archiving"

    monkeypatch.setattr(ws_model, "SessionStatus", _GrownStatus)

    assert "archiving" in ws_model.NON_ENDED_STATUSES()
    assert "archiving" in ws_model.NON_ENDED_STATUSES_NOT_PAUSED()
    assert set(ws_model.NON_ENDED_STATUSES()) == {"created", "running", "waiting", "paused", "archiving"}
    assert set(ws_model.NON_ENDED_STATUSES_NOT_PAUSED()) == {"created", "running", "waiting", "archiving"}


def test_each_call_returns_a_fresh_list() -> None:
    first = ws_model.NON_ENDED_STATUSES()
    first.append("ended")

    assert "ended" not in ws_model.NON_ENDED_STATUSES()
    assert ws_model.NON_ENDED_STATUSES_NOT_PAUSED() is not ws_model.NON_ENDED_STATUSES_NOT_PAUSED()
