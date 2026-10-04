"""Session compaction: guards and the marker write (S1 P2 Task 12).

Spec: docs/superpowers/ux-revamp/02-s1-design.md section 5.

The LLM seam is injected so these stay milliseconds long and need no
provider. The endpoint supplies the real one.
"""

import json
from datetime import UTC, datetime

import pytest

from primer.model.chat import Message, TextPart
from primer.model.except_ import ConflictError, ValidationError
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


class TestKeptTail:
    """The marker records the tail the compactor kept, or the fold would drop it (the tier-2 data-loss fix)."""

    @staticmethod
    def _result(summary, kept_tail):
        class _R:
            summary_text = summary
            tokens_before = 100
            tokens_after = 10
            model_name = "test-model"

        _R.kept_tail = kept_tail
        return _R

    async def test_the_marker_carries_the_kept_tail_and_the_reader_puts_it_after_the_summary(self):
        from primer.workspace.session import reconstruct_compacted_history

        io = _IO()
        kept = [Message(role="user", parts=[TextPart(text="the unanswered question")])]

        async def run(_history):
            return self._result("rolled up", kept)

        await compact_session(row=_row(), workspace_io=io, history=[], run_compaction=run)
        payload = json.loads(io.lines[0])["payload"]
        assert [m["parts"][0]["text"] for m in payload["kept_tail_messages"]] == ["the unanswered question"]

        shown = reconstruct_compacted_history([line.decode() for line in io.lines])
        assert [(m.role, m.parts[0].text) for m in shown] == [
            ("assistant", "rolled up"), ("user", "the unanswered question"),
        ]

    async def test_a_marker_with_nothing_kept_has_no_tail_key(self):
        io = _IO()

        async def run(_history):
            return self._result("rolled up", [])

        await compact_session(row=_row(), workspace_io=io, history=[], run_compaction=run)
        assert "kept_tail_messages" not in json.loads(io.lines[0])["payload"]

    async def test_a_line_written_while_the_summariser_ran_is_carried_into_the_marker(self):
        """The summarising call takes seconds and a steer can land in that time; the marker folds every line before it."""
        from primer.workspace.session import reconstruct_compacted_history

        io = _IO()
        old = [Message(role="user", parts=[TextPart(text="q")]), Message(role="assistant", parts=[TextPart(text="a")])]
        steer = Message(role="user", parts=[TextPart(text="A STEER WRITTEN DURING THE SUMMARISER CALL")])

        async def run(_history):
            return self._result("rolled up", [])

        async def reload():
            return [*old, steer]

        await compact_session(row=_row(), workspace_io=io, history=old, run_compaction=run, reload_history=reload)
        shown = reconstruct_compacted_history([line.decode() for line in io.lines])
        assert [(m.role, m.parts[0].text) for m in shown] == [
            ("assistant", "rolled up"), ("user", "A STEER WRITTEN DURING THE SUMMARISER CALL"),
        ]

    async def test_a_history_that_changed_under_the_summariser_carries_nothing_rather_than_something_wrong(self):
        io = _IO()
        old = [Message(role="user", parts=[TextPart(text="q")]), Message(role="assistant", parts=[TextPart(text="a")])]

        async def run(_history):
            return self._result("rolled up", [])

        async def reload():  # a rewind or another marker: the prefix no longer matches
            return [Message(role="assistant", parts=[TextPart(text="a different summary")]),
                    Message(role="user", parts=[TextPart(text="x")]), Message(role="user", parts=[TextPart(text="y")])]

        await compact_session(row=_row(), workspace_io=io, history=old, run_compaction=run, reload_history=reload)
        assert "kept_tail_messages" not in json.loads(io.lines[0])["payload"]

    async def test_the_marker_records_the_verdict(self):
        io = _IO()

        async def run(_history):
            r = self._result("rolled up", [])
            r.outcome, r.unreducible, r.trigger_tokens = "insufficient", "over_trigger", 82_627
            return r

        await compact_session(row=_row(), workspace_io=io, history=[], run_compaction=run)
        payload = json.loads(io.lines[0])["payload"]
        assert (payload["outcome"], payload["unreducible"], payload["trigger_tokens"]) == ("insufficient", "over_trigger", 82_627)

    async def test_a_compaction_that_summarised_nothing_writes_no_marker(self):
        """An empty summary would fold the whole history into nothing: refuse, and write nothing."""
        io = _IO()

        async def run(_history):
            return self._result("", [])

        with pytest.raises(ValidationError, match="nothing to compact"):
            await compact_session(row=_row(), workspace_io=io, history=[], run_compaction=run)
        assert io.lines == []
