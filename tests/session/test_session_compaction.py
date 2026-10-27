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


class _Sessions:
    """A session-row storage whose ``patch_if`` honours its predicate, like the real backends (the fence is the guarantee)."""

    def __init__(self, row: WorkspaceSession):
        self.row = row

    async def patch_if(self, id, patch=None, *, where, set_paths=None, conn=None):
        del set_paths, conn
        for key, wanted in where.items():
            if getattr(self.row, key) not in wanted:
                return None
        self.row = self.row.model_copy(update=dict(patch or {}))
        return self.row


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
        row = _row()
        result = await compact_session(
            row=row, workspace_io=io, history=[],
            run_compaction=_fake_compaction, sessions=_Sessions(row),
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
        row = _row()
        await compact_session(
            row=row, workspace_io=io, history=[],
            run_compaction=_fake_compaction, sessions=_Sessions(row),
        )
        payload = json.loads(io.lines[0].decode())["payload"]
        for key in (
            "summary", "replaced_from_seq", "replaced_to_seq", "model",
            "tokens_before", "tokens_after", "created_at",
        ):
            assert key in payload, f"marker payload missing {key!r}"

    async def test_first_compaction_replaces_from_one(self):
        io = _IO()
        row = _row(next_unprocessed_seq=0)
        await compact_session(
            row=row, workspace_io=io, history=[],
            run_compaction=_fake_compaction, sessions=_Sessions(row),
        )
        payload = json.loads(io.lines[0].decode())["payload"]
        assert payload["replaced_from_seq"] == 1

    async def test_later_compaction_starts_at_the_drain_cursor(self):
        io = _IO()
        row = _row(next_unprocessed_seq=4)
        await compact_session(
            row=row, workspace_io=io, history=[],
            run_compaction=_fake_compaction, sessions=_Sessions(row),
        )
        payload = json.loads(io.lines[0].decode())["payload"]
        assert payload["replaced_from_seq"] == 4

    async def test_returns_the_token_counts_for_the_ui(self):
        io = _IO()
        row = _row()
        result = await compact_session(
            row=row, workspace_io=io, history=[],
            run_compaction=_fake_compaction, sessions=_Sessions(row),
        )
        assert (result.tokens_before, result.tokens_after) == (100, 10)
        assert result.summary == "rolled up"


class TestFencedReservation:
    """The marker's seq is RESERVED, not seeded from a read (S2a PR-5b, D-A1-02): the reservation is the row's
    ``last_seq`` write, and a row that moved since the caller read it rejects the fence and writes nothing."""

    async def test_the_reservation_is_the_rows_last_seq_write(self):
        io = _IO()
        row = _row()
        sessions = _Sessions(row)
        outcome = await compact_session(
            row=row, workspace_io=io, history=[],
            run_compaction=_fake_compaction, sessions=sessions,
        )
        assert outcome.compaction_marker_seq == 7
        assert sessions.row.last_seq == 7, "the reservation is the row's last_seq write"

    async def test_a_row_that_parked_while_the_summariser_ran_rejects_the_reservation_and_writes_nothing(self):
        """The row the route re-reads under the lock already holds the park: the fence (idle-unparked plus
        last_seq == the row just read) rejects it, so no marker and no row write."""
        io = _IO()
        moved = _row(last_seq=7, parked_status="parked", parked_state={"kind": "ask_user"})
        sessions = _Sessions(moved)

        with pytest.raises(ConflictError, match="no longer idle"):
            await compact_session(
                row=moved, workspace_io=io, history=[],
                run_compaction=_fake_compaction, sessions=sessions,
            )
        assert io.lines == [], "a rejected reservation writes no marker"
        assert (sessions.row.parked_status, sessions.row.last_seq) == ("parked", 7), "the park is not written over"

    async def test_a_row_whose_last_seq_moved_rejects_the_reservation_and_writes_nothing(self):
        """The fence also carries last_seq == the row the caller read: a concurrent writer that only moved
        the cursor rejects it too."""
        io = _IO()
        stale = _row()
        sessions = _Sessions(_row(last_seq=7))

        with pytest.raises(ConflictError, match="no longer idle"):
            await compact_session(
                row=stale, workspace_io=io, history=[],
                run_compaction=_fake_compaction, sessions=sessions,
            )
        assert io.lines == [], "a rejected reservation writes no marker"
        assert sessions.row.last_seq == 7


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

        row = _row()
        await compact_session(row=row, workspace_io=io, history=[], run_compaction=run,
                              sessions=_Sessions(row))
        payload = json.loads(io.lines[0])["payload"]
        assert [m["parts"][0]["text"] for m in payload["kept_tail_messages"]] == ["the unanswered question"]

        shown = reconstruct_compacted_history([line.decode() for line in io.lines])
        assert [(m.role, m.parts[0].text) for m in shown] == [
            ("assistant", "rolled up"), ("user", "the unanswered question"),
        ]

    async def test_a_summary_that_sits_after_the_kept_user_run_is_recorded_and_read_back_in_place(self):
        from primer.workspace.session import reconstruct_compacted_history

        io = _IO()
        kept = [Message(role="user", parts=[TextPart(text="THE QUESTION")]),
                Message(role="assistant", parts=[TextPart(text="newest round")])]

        async def run(_history):
            r = self._result("rolled up", kept)
            r.summary_after = 1
            return r

        row = _row()
        await compact_session(row=row, workspace_io=io, history=[], run_compaction=run,
                              sessions=_Sessions(row))
        assert json.loads(io.lines[0])["payload"]["summary_after"] == 1
        shown = reconstruct_compacted_history([line.decode() for line in io.lines])
        assert [(m.role, m.parts[0].text) for m in shown] == [
            ("user", "THE QUESTION"), ("assistant", "rolled up"), ("assistant", "newest round"),
        ]

    async def test_a_marker_with_nothing_kept_has_no_tail_key(self):
        io = _IO()

        async def run(_history):
            return self._result("rolled up", [])

        row = _row()
        await compact_session(row=row, workspace_io=io, history=[], run_compaction=run,
                              sessions=_Sessions(row))
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

        row = _row()
        await compact_session(
            row=row, workspace_io=io, history=old, run_compaction=run,
            reload_history=reload, sessions=_Sessions(row),
        )
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

        row = _row()
        await compact_session(
            row=row, workspace_io=io, history=old, run_compaction=run,
            reload_history=reload, sessions=_Sessions(row),
        )
        assert "kept_tail_messages" not in json.loads(io.lines[0])["payload"]

    async def test_the_marker_records_the_verdict(self):
        io = _IO()

        async def run(_history):
            r = self._result("rolled up", [])
            r.outcome, r.unreducible, r.trigger_tokens = "insufficient", "over_trigger", 82_627
            return r

        row = _row()
        await compact_session(row=row, workspace_io=io, history=[], run_compaction=run,
                              sessions=_Sessions(row))
        payload = json.loads(io.lines[0])["payload"]
        assert (payload["outcome"], payload["unreducible"], payload["trigger_tokens"]) == ("insufficient", "over_trigger", 82_627)

    async def test_the_outcome_and_the_marker_say_what_was_done_to_the_summarisers_input(self):
        io = _IO()
        reduced = {"pruned": 2, "folded_chunks": 0, "truncated_parts": 1}

        async def run(_history):
            r = self._result("rolled up", [])
            r.summary_input_reduced = reduced
            return r

        row = _row()
        outcome = await compact_session(row=row, workspace_io=io, history=[], run_compaction=run,
                                        sessions=_Sessions(row))
        assert outcome.summary_input_reduced == reduced
        assert json.loads(io.lines[0])["payload"]["summary_input_reduced"] == reduced

    async def test_a_compaction_whose_summariser_did_not_overflow_has_no_reduction_anywhere(self):
        io = _IO()

        async def run(_history):
            return self._result("rolled up", [])

        row = _row()
        outcome = await compact_session(row=row, workspace_io=io, history=[], run_compaction=run,
                                        sessions=_Sessions(row))
        assert outcome.summary_input_reduced is None
        assert "summary_input_reduced" not in json.loads(io.lines[0])["payload"]

    async def test_a_compaction_that_summarised_nothing_writes_no_marker(self):
        """An empty summary would fold the whole history into nothing: refuse, and write nothing."""
        io = _IO()

        async def run(_history):
            return self._result("", [])

        with pytest.raises(ValidationError, match="nothing to compact"):
            row = _row()
            await compact_session(row=row, workspace_io=io, history=[], run_compaction=run,
                                  sessions=_Sessions(row))
        assert io.lines == []

    async def test_the_refusal_names_the_strategys_reason_and_carries_it_as_a_problem_extension(self):
        from primer.session.compaction import NothingToCompact

        async def run(_history):
            r = self._result("", [])
            r.unreducible = "protected_over_budget"
            return r

        with pytest.raises(NothingToCompact) as refused:
            row = _row()
            await compact_session(row=row, workspace_io=_IO(), history=[], run_compaction=run,
                                  sessions=_Sessions(row))
        assert refused.value.reason == "protected_over_budget"
        assert refused.value.problem_extensions == {"reason": "protected_over_budget"}
        assert "fills the context window" in str(refused.value) and "no earlier history" not in str(refused.value)

    async def test_a_history_that_changed_under_the_summariser_is_logged_not_silently_dropped(self, caplog):
        import logging

        async def run(_history):
            return self._result("rolled up", [])

        old = [Message(role="user", parts=[TextPart(text="q")])]

        async def reload():  # a different shape: the prefix no longer matches
            return [Message(role="assistant", parts=[TextPart(text="x")]), Message(role="user", parts=[TextPart(text="y")])]

        with caplog.at_level(logging.WARNING, logger="primer.session.compaction"):
            row = _row()
            await compact_session(row=row, workspace_io=_IO(), history=old, run_compaction=run,
                                  reload_history=reload, sessions=_Sessions(row))
        assert any("history changed under a manual compaction" in r.getMessage() for r in caplog.records)
