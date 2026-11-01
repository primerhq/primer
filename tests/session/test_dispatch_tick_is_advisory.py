"""AG-M1: a tick publish that fails never fails, strands or rewinds a turn.

The tick only tells the tap the log grew (the tap catches up on its next read), as ``_publish_terminal`` already treats its own
publish. A NOTIFY on a reset pool connection used to (a) end an answered turn ENDED/failed when the per-record tick raised, and
(b) escape the turn from the final flush's tick, before ``last_seq`` was written back, leaving the row RUNNING with no turn and
``last_seq`` behind the records on disk.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from primer.bus.in_memory import InMemoryEventBus
from primer.model.chat import Done, TextDelta
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.session.delegation import DelegationRecorder
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn
from primer.session.persistence import WorkspaceMessageWriter
from tests.conftest import _FakeStorageProvider
from tests.session.test_dispatch import FakeWorkspaceIO, _make_lease


class _Clean:
    last_done_reason = "stop"

    async def invoke(self, messages: list[Any], **kwargs: Any):
        yield TextDelta(text="the answer", index=0)
        yield Done(stop_reason="stop", raw_reason="stop")


class _FlakyTickBus(InMemoryEventBus):
    """Raises on the Nth publish of a tick key (a NOTIFY on a pool connection that was reset)."""

    def __init__(self, fail_on: int):
        super().__init__()
        self.ticks = 0
        self.fail_on = fail_on

    async def publish(self, event_key, payload=None):
        if event_key.endswith(":tick"):
            self.ticks += 1
            if self.ticks == self.fail_on:
                raise ConnectionResetError("connection reset by peer")
        return await super().publish(event_key, payload)


async def _turn(fail_on: int):
    sp = _FakeStorageProvider()
    sessions = sp.get_storage(WorkspaceSession)
    await sessions.create(WorkspaceSession(
        id="s1", workspace_id="w1", binding=AgentSessionBinding(agent_id="a"), status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc), turn_status="claimable", last_seq=10,
    ))
    bus = _FlakyTickBus(fail_on)
    await bus.initialize()
    io = FakeWorkspaceIO()

    async def build(_s):
        return _Clean()

    outcome = await run_one_session_turn(_make_lease("s1"), SessionDispatchDeps(
        storage_provider=sp, workspace_io=io, event_bus=bus, build_executor=build,
    ))
    seqs = [json.loads(ln)["seq"] for ln in io.read_lines("s1")]
    return outcome, await sessions.get("s1"), seqs


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_on", [1, 2, 3], ids=["first-record-tick", "last-record-tick", "final-flush-tick"])
async def test_a_failed_tick_leaves_the_turn_resting_with_last_seq_covering_the_log(fail_on):
    outcome, row, seqs = await _turn(fail_on)
    assert seqs == [11, 12]
    assert (outcome.success, outcome.drop_lease) == (True, True)
    assert (row.status, row.ended_reason, row.turn_status) == (SessionStatus.WAITING, None, "idle")
    assert row.last_seq == 12


@pytest.mark.asyncio
async def test_a_failed_tick_of_a_delegated_record_does_not_escape():
    """The delegation recorder's per-record tick, under a subagent run: the record is appended and the run goes on."""
    io = FakeWorkspaceIO()
    writer = WorkspaceMessageWriter(workspace_io=io, session_id="s1", start_seq=0)
    bus = _FlakyTickBus(fail_on=1)
    await bus.initialize()
    recorder = DelegationRecorder(writer=writer, event_bus=bus, session_id="s1")
    await recorder.on_event(TextDelta(text="child says", index=0), delegate_tool_call_id="c1", delegate_run_id="r1")
    await recorder.on_event(Done(stop_reason="stop", raw_reason="stop"), delegate_tool_call_id="c1", delegate_run_id="r1")
    await writer.flush()
    assert [json.loads(ln)["kind"] for ln in io.read_lines("s1")] == ["assistant_token", "done"]
