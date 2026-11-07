"""Abandoning a session's open gate (S1 P3 Task 21, amendment M15).

Ported from chat's abandon chokepoint. Abandoning is one helper rather
than three call sites because the ordering is an invariant: the log
must never carry an unpaired tool_use, so the synthetic rejected
tool_result is written before the terminal that closes the turn.

Clearing parked_state as well as parked_status is the M15 half: a stale
parked_session subscription that fires afterwards must find no park to
resume, so it skips and self-deletes instead of reviving a turn that
has already been closed cancelled.
"""

import json
from datetime import UTC, datetime

import pytest

from primer.model.except_ import ConflictError
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.abandon import abandon_session_gate
from tests.conftest import _InMemoryStorage


def _row(**kw):
    base = {
        "id": "s", "workspace_id": "w",
        "binding": AgentSessionBinding(agent_id="a"),
        "status": SessionStatus.RUNNING,
        "created_at": datetime.now(UTC),
        "last_seq": 5,
        "parked_status": "parked",
        "parked_state": {
            "tool_call_id": "tc-9",
            "yielded": {"tool_name": "ask_user"},
            "mode": "ask_user",
        },
        "parked_event_key": "ask_user:s:tc-9",
    }
    base.update(kw)
    return WorkspaceSession(**base)


class _Sessions(_InMemoryStorage[WorkspaceSession]):
    def __init__(self, row):
        super().__init__(WorkspaceSession)
        self.row = row

    @property
    def row(self):
        return self._data["s"]

    @row.setter
    def row(self, row):
        self._data[row.id] = row


class _IO:
    def __init__(self):
        self.lines: list[bytes] = []

    async def append_message_line(self, session_id, line):
        self.lines.append(line)


def _records(io):
    """The writer batches, so one append can carry several jsonl lines."""
    blob = b"".join(io.lines).decode()
    return [json.loads(line) for line in blob.splitlines() if line.strip()]


class TestAbandonGate:
    async def test_writes_the_result_before_the_terminal(self):
        """The log must never carry an unpaired tool_use."""
        io = _IO()
        await abandon_session_gate(
            sessions=_Sessions(_row()), workspace_io=io, row=_row(),
            reason="binding switched",
        )
        kinds = [rec["kind"] for rec in _records(io)]
        assert kinds == ["tool_result", "cancelled"]

    async def test_seqs_continue_the_session_monotonically(self):
        io = _IO()
        await abandon_session_gate(
            sessions=_Sessions(_row()), workspace_io=io, row=_row(),
            reason="binding switched",
        )
        assert [rec["seq"] for rec in _records(io)] == [6, 7]

    async def test_result_is_an_error_carrying_the_gate_tool_call_id(self):
        io = _IO()
        await abandon_session_gate(
            sessions=_Sessions(_row()), workspace_io=io, row=_row(),
            reason="binding switched",
        )
        result = _records(io)[0]
        assert result["payload"]["error"] is True
        # call_id/output, NOT id/result - primer/session/timeline.py's
        # TOOL_CALL pairing looks up payload["call_id"] specifically
        # (01a05350; matches the live-turn write shape in
        # primer/session/persistence.py's _ExecutorToolResult handler).
        assert result["payload"]["call_id"] == "tc-9"
        assert "binding switched" in result["payload"]["output"]

    async def test_result_pairs_with_its_tool_call_in_the_timeline(self):
        """01a05350: an abandoned gate's synthesized TOOL_RESULT must
        actually close its TOOL_CALL node in the timeline view, not fall
        through as an unpaired record."""
        from primer.session.timeline import build_turn_timeline

        io = _IO()
        tool_call_line = json.dumps({
            "seq": 5,
            "kind": "tool_call",
            "created_at": datetime.now(UTC).isoformat(),
            "payload": {"id": "tc-9", "name": "ask_user", "arguments": {}},
        })
        await abandon_session_gate(
            sessions=_Sessions(_row()), workspace_io=io, row=_row(),
            reason="binding switched",
        )
        abandoned_lines = [json.dumps(rec) for rec in _records(io)]

        tl = build_turn_timeline(
            message_lines=[tool_call_line, *abandoned_lines],
            turn_log_lines=[], turn_no=0,
        )
        tool_node = next(
            c for c in tl["children"] if c.get("tool_call_id") == "tc-9"
        )
        assert tool_node["status"] == "error"
        assert tool_node["result"] is not None
        assert "binding switched" in tool_node["result"]["output"]

    async def test_clears_both_park_fields_and_advances_last_seq(self):
        """M15: leaving parked_state populated would look resumable."""
        row = _row(
            parked_tool_batches=True,
            resumable_at=datetime.now(UTC),
            parked_state={
                "graph_checkpoint": {"pending_tool_waits": [{
                    "node_id": "A",
                    "outstanding_task_ids": ["s/A:tool:0:1"],
                    "notifying_results": [],
                }]},
                "tool_call_id": "tc-9",
                "yielded": {"tool_name": "ask_user"},
                "mode": "ask_user",
            },
        )
        sessions = _Sessions(row)
        updated = await abandon_session_gate(
            sessions=sessions, workspace_io=_IO(), row=row,
            reason="binding switched",
        )
        assert updated.parked_status is None
        assert updated.parked_state is None
        assert updated.parked_tool_batches is None
        assert updated.resumable_at is None
        assert updated.last_seq == 7

    async def test_noop_when_nothing_is_parked(self):
        row = _row(parked_status=None, parked_state=None)
        io = _IO()
        updated = await abandon_session_gate(
            sessions=_Sessions(row), workspace_io=io, row=row,
            reason="binding switched",
        )
        assert io.lines == []
        assert updated.last_seq == 5

    async def test_field_patch_preserves_concurrent_unrelated_change(self):
        row = _row()
        sessions = _Sessions(row)
        real_patch = sessions.patch_if

        async def race_then_patch(*args, **kwargs):
            sessions.row = sessions.row.model_copy(update={"cancel_requested": True})
            return await real_patch(*args, **kwargs)

        sessions.patch_if = race_then_patch
        updated = await abandon_session_gate(
            sessions=sessions, workspace_io=_IO(), row=row,
            reason="binding switched",
        )
        assert updated.cancel_requested is True
        assert updated.parked_status is None and updated.last_seq == 7

    async def test_moved_park_refuses_the_stale_clear(self):
        row = _row()
        sessions = _Sessions(row)
        real_patch = sessions.patch_if

        async def move_then_patch(*args, **kwargs):
            sessions.row = sessions.row.model_copy(update={
                "parked_event_key": "ask_user:s:new",
            })
            return await real_patch(*args, **kwargs)

        sessions.patch_if = move_then_patch
        with pytest.raises(ConflictError, match="changed while its gate was being abandoned"):
            await abandon_session_gate(
                sessions=sessions, workspace_io=_IO(), row=row,
                reason="binding switched",
            )
        assert sessions.row.parked_event_key == "ask_user:s:new"
