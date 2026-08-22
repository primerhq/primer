"""Session compaction: guards and the marker write (S1 P2 Task 12).

Spec: docs/superpowers/ux-revamp/02-s1-design.md section 5.

The LLM seam is injected so these stay milliseconds long and need no
provider. The endpoint supplies the real one.
"""

import json
from datetime import UTC, datetime

import pytest

from primer.model.except_ import ConflictError
from primer.model.workspace_session import (
    AgentSessionBinding,
    GraphSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.compaction import compact_session, guard_compactable


def _row(**kw):
    base = {
        "id": "s", "workspace_id": "w",
        "binding": AgentSessionBinding(agent_id="a"),
        "status": SessionStatus.WAITING,
        "created_at": datetime.now(UTC),
        "last_seq": 6,
    }
    base.update(kw)
    return WorkspaceSession(**base)


class _IO:
    def __init__(self):
        self.lines: list[bytes] = []

    async def append_message_line(self, session_id, line):
        self.lines.append(line)


async def _fake_compaction(history):
    class _R:
        summary_text = "rolled up"
        tokens_before = 100
        tokens_after = 10
        model_name = "test-model"

    return _R()


class TestGuard:
    def test_rejects_a_running_turn(self):
        with pytest.raises(ConflictError):
            guard_compactable(_row(turn_status="running"))

    def test_rejects_a_parked_session(self):
        """A park is mid-turn: its resume still needs the history."""
        with pytest.raises(ConflictError):
            guard_compactable(_row(parked_status="parked"))

    def test_rejects_a_graph_binding(self):
        """Graph internals see graph state, not session history, so a
        graph binding has no conversation to fold."""
        with pytest.raises(ConflictError):
            guard_compactable(_row(binding=GraphSessionBinding(graph_id="g")))

    def test_passes_an_idle_agent_session(self):
        guard_compactable(_row())


class TestCompactSession:
    async def test_appends_a_marker_after_last_seq(self):
        io = _IO()
        result = await compact_session(
            row=_row(), workspace_io=io, history=[],
            run_compaction=_fake_compaction,
        )
        assert result.compaction_marker_seq == 7  # last_seq 6 + 1
        written = json.loads(io.lines[0].decode())
        assert written["kind"] == "compaction_marker"
        assert written["seq"] == 7
        assert written["payload"]["summary"] == "rolled up"
        assert written["payload"]["replaced_to_seq"] == 6

    async def test_marker_payload_carries_the_documented_shape(self):
        """The reader and the UI both key off these fields."""
        io = _IO()
        await compact_session(
            row=_row(), workspace_io=io, history=[],
            run_compaction=_fake_compaction,
        )
        payload = json.loads(io.lines[0].decode())["payload"]
        for key in (
            "summary", "replaced_from_seq", "replaced_to_seq", "model",
            "tokens_before", "tokens_after", "created_at",
        ):
            assert key in payload, f"marker payload missing {key!r}"

    async def test_first_compaction_replaces_from_one(self):
        io = _IO()
        await compact_session(
            row=_row(next_unprocessed_seq=0), workspace_io=io, history=[],
            run_compaction=_fake_compaction,
        )
        payload = json.loads(io.lines[0].decode())["payload"]
        assert payload["replaced_from_seq"] == 1

    async def test_later_compaction_starts_at_the_drain_cursor(self):
        io = _IO()
        await compact_session(
            row=_row(next_unprocessed_seq=4), workspace_io=io, history=[],
            run_compaction=_fake_compaction,
        )
        payload = json.loads(io.lines[0].decode())["payload"]
        assert payload["replaced_from_seq"] == 4

    async def test_returns_the_token_counts_for_the_ui(self):
        io = _IO()
        result = await compact_session(
            row=_row(), workspace_io=io, history=[],
            run_compaction=_fake_compaction,
        )
        assert (result.tokens_before, result.tokens_after) == (100, 10)
        assert result.summary == "rolled up"
