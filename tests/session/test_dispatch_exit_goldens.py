"""Characterization of the session dispatch exits (Part 2 Task 43).

Pins what each exit of ``run_one_session_turn`` writes and announces today: the ``(seq, kind)`` of every record, the
final row fields, the tick seqs and the terminal payloads, for a clean, a cancelled, a parked and a failed
interactive turn and for a Cancel found at claim time; and the ``_post_turn_status`` table (every interactive
outcome, and every autonomous outcome that is not a rest).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import pytest

from primer.bus.in_memory import InMemoryEventBus
from primer.model.chat import Done, TextDelta
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.model.yield_ import Yielded, YieldToWorker
from primer.session.dispatch import SessionDispatchDeps, _post_turn_status, run_one_session_turn
from tests.conftest import _FakeStorageProvider
from tests.session.test_dispatch import FakeWorkspaceIO, _make_lease


class _RecBus(InMemoryEventBus):
    def __init__(self) -> None:
        super().__init__()
        self.published: list[tuple[str, Any]] = []

    async def publish(self, event_key, payload=None):
        self.published.append((event_key, payload))
        return await super().publish(event_key, payload)

    def ticks(self, sid: str) -> list[int]:
        return [p["seq"] for k, p in self.published if k == f"session:{sid}:tick"]

    def terminals(self, sid: str) -> list[Any]:
        return [p for k, p in self.published if k == f"session:{sid}:terminal"]


class _Events:
    def __init__(self, events: list[Any], last_done_reason: str | None = None) -> None:
        self._events = events
        self.last_done_reason = last_done_reason

    async def invoke(self, messages, **kwargs):
        for ev in self._events:
            if isinstance(ev, BaseException):
                raise ev
            yield ev


class _CancelMidStream:
    """Streams a partial answer, then a Cancel lands (the flag on the row, then the bus key), as ``cancel_session`` writes it.

    Owns the Stop event, as the agent executors do: it ends its stream once the watcher has set it.
    """

    was_interrupted = False

    def __init__(self, sessions, bus, sid) -> None:
        self._sessions, self._bus, self._sid = sessions, bus, sid

    def bind_interrupt_event(self, event: asyncio.Event) -> None:
        self._stop = event

    async def invoke(self, messages, **kwargs):
        yield TextDelta(text="partial", index=0)
        await asyncio.sleep(0.05)  # the turn's cancel watcher subscribes
        row = await self._sessions.get(self._sid)
        row.cancel_requested = True
        await self._sessions.update(row)
        await self._bus.publish(f"session:{self._sid}:cancel", {})
        await asyncio.wait_for(self._stop.wait(), 5)
        self.was_interrupted = True


async def _run(executor_factory, **row_fields):
    sp = _FakeStorageProvider()
    sessions = sp.get_storage(WorkspaceSession)
    fields = dict(
        id="s1", workspace_id="w1", binding=AgentSessionBinding(agent_id="a"), status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc), turn_status="claimable", last_seq=10,
    )
    fields.update(row_fields)
    await sessions.create(WorkspaceSession(**fields))
    bus = _RecBus()
    await bus.initialize()
    io = FakeWorkspaceIO()
    built: list[Any] = []

    async def build(_s):
        ex = executor_factory(sessions, bus)
        built.append(ex)
        return ex

    outcome = await run_one_session_turn(_make_lease("s1"), SessionDispatchDeps(
        storage_provider=sp, workspace_io=io, event_bus=bus, build_executor=build,
    ))
    row = await sessions.get("s1")
    records = [json.loads(ln) for ln in io.read_lines("s1")]
    return outcome, row, [(r["seq"], r["kind"]) for r in records], bus, built


def _row_fields(row: WorkspaceSession) -> tuple:
    return (row.status, row.ended_reason, row.turn_status, row.last_seq, row.next_unprocessed_seq, row.turn_started_at)


@pytest.mark.asyncio
async def test_clean_interactive_turn():
    outcome, row, seq_kinds, bus, _ = await _run(
        lambda s, b: _Events([TextDelta(text="the answer", index=0), Done(stop_reason="stop", raw_reason="stop")], "stop"),
    )
    assert (outcome.success, outcome.drop_lease, outcome.park) == (True, True, None)
    assert seq_kinds == [(11, "assistant_token"), (12, "done")]
    assert _row_fields(row) == (SessionStatus.WAITING, None, "idle", 12, 13, None)
    assert bus.ticks("s1") == [11, 12, 12]
    assert bus.terminals("s1") == [{"status": "waiting", "ended_reason": None}]


@pytest.mark.asyncio
async def test_cancelled_interactive_turn():
    outcome, row, seq_kinds, bus, _ = await _run(lambda s, b: _CancelMidStream(s, b, "s1"))
    assert (outcome.success, outcome.drop_lease, outcome.park) == (True, True, None)
    assert seq_kinds == [(11, "assistant_token"), (12, "cancelled")]
    assert _row_fields(row) == (SessionStatus.ENDED, "cancelled", "idle", 12, 13, None)
    assert bus.ticks("s1") == [12]
    assert bus.terminals("s1") == [{"status": "ended", "ended_reason": "cancelled"}]


@pytest.mark.asyncio
async def test_parked_interactive_turn():
    park = YieldToWorker(Yielded(tool_name="wait_tool", event_key="timer:tc1"), tool_call_id="tc1")
    outcome, row, seq_kinds, bus, _ = await _run(lambda s, b: _Events([TextDelta(text="thinking", index=0), park]))
    assert (outcome.success, outcome.drop_lease) == (True, True)
    assert outcome.park is not None and outcome.park.parked_event_key == "timer:tc1"
    assert seq_kinds == [(11, "yielded")]
    assert _row_fields(row) == (SessionStatus.RUNNING, None, "idle", 11, 0, None)
    assert bus.ticks("s1") == [11]
    assert bus.terminals("s1") == []


@pytest.mark.asyncio
async def test_failed_interactive_turn():
    outcome, row, seq_kinds, bus, _ = await _run(
        lambda s, b: _Events([TextDelta(text="half an answer", index=0), RuntimeError("boom")]),
    )
    assert (outcome.success, outcome.drop_lease, outcome.park) == (False, True, None)
    assert seq_kinds == [(11, "assistant_token"), (12, "error")]
    assert _row_fields(row) == (SessionStatus.ENDED, "failed", "idle", 12, 13, None)
    assert bus.ticks("s1") == [12]
    assert bus.terminals("s1") == [{"status": "ended", "ended_reason": "failed"}]


@pytest.mark.asyncio
async def test_cancel_found_at_claim_row_and_outcome():
    """The row fields and the release a Cancel already on the row at claim time leaves (the turn never runs)."""
    outcome, row, _, _, built = await _run(lambda s, b: _Events([]), cancel_requested=True, turn_status="running",
                                           turn_started_at=datetime.now(timezone.utc))
    assert (outcome.success, outcome.drop_lease, outcome.park) == (True, True, None)
    assert built == []
    assert (row.status, row.ended_reason, row.turn_status, row.turn_started_at) == (SessionStatus.ENDED, "cancelled", "idle", None)
    assert row.ended_at is not None


_INTERACTIVE_TABLE = {
    # (last_done_reason, agent_status) -> (status, ended_reason), autonomous=False
    ("stop", None): (SessionStatus.WAITING, None),
    ("end_turn", None): (SessionStatus.WAITING, None),
    ("stop_sequence", None): (SessionStatus.WAITING, None),
    ("tool_use", None): (SessionStatus.RUNNING, None),
    ("max_tokens", None): (SessionStatus.WAITING, None),
    ("error", None): (SessionStatus.ENDED, "failed"),
    ("content_filter", None): (SessionStatus.WAITING, None),
    ("graph_ended", None): (SessionStatus.ENDED, "completed"),
    ("graph_failed", None): (SessionStatus.ENDED, "failed"),
    ("tool_turn_cap", None): (SessionStatus.WAITING, None),
    (None, None): (SessionStatus.ENDED, "completed"),
    ("weird", None): (SessionStatus.ENDED, "completed"),
    ("stop", SessionStatus.WAITING): (SessionStatus.WAITING, None),
    ("max_tokens", SessionStatus.WAITING): (SessionStatus.WAITING, None),
    (None, SessionStatus.WAITING): (SessionStatus.WAITING, None),
    ("stop", SessionStatus.ENDED): (SessionStatus.ENDED, "completed"),
    ("error", SessionStatus.ENDED): (SessionStatus.ENDED, "failed"),
    ("tool_turn_cap", SessionStatus.ENDED): (SessionStatus.ENDED, "completed"),
    ("stop", SessionStatus.RUNNING): (SessionStatus.WAITING, None),
    ("tool_use", SessionStatus.RUNNING): (SessionStatus.RUNNING, None),
    ("max_tokens", SessionStatus.RUNNING): (SessionStatus.WAITING, None),
    ("stop", SessionStatus.PAUSED): (SessionStatus.WAITING, None),
    (None, SessionStatus.PAUSED): (SessionStatus.ENDED, "completed"),
}


@pytest.mark.parametrize("case", sorted(_INTERACTIVE_TABLE, key=repr))
def test_post_turn_status_interactive_table(case):
    assert _post_turn_status(*case, autonomous=False) == _INTERACTIVE_TABLE[case]


_AUTONOMOUS_ENDS = {
    # Every autonomous outcome that is not a rest today, with its reason.
    ("stop", None): (SessionStatus.ENDED, "completed"),
    ("end_turn", None): (SessionStatus.ENDED, "completed"),
    ("stop_sequence", None): (SessionStatus.ENDED, "completed"),
    ("error", None): (SessionStatus.ENDED, "failed"),
    ("graph_ended", None): (SessionStatus.ENDED, "completed"),
    ("graph_failed", None): (SessionStatus.ENDED, "failed"),
    ("tool_turn_cap", None): (SessionStatus.ENDED, "tool_turn_cap"),
    (None, None): (SessionStatus.ENDED, "completed"),
    ("stop", SessionStatus.ENDED): (SessionStatus.ENDED, "completed"),
    ("error", SessionStatus.ENDED): (SessionStatus.ENDED, "failed"),
    ("tool_use", None): (SessionStatus.RUNNING, None),
}


@pytest.mark.parametrize("case", sorted(_AUTONOMOUS_ENDS, key=repr))
def test_post_turn_status_autonomous_non_rest_outcomes(case):
    assert _post_turn_status(*case, autonomous=True) == _AUTONOMOUS_ENDS[case]
