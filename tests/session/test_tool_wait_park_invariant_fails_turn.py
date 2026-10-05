"""A deterministic bookkeeping break while a live turn parks ends the session FAILED (S1b follow-up).

The park arms of ``run_one_session_turn`` (``except YieldToWorker`` with a co-pending tool_wait batch, and
``except ToolWaitPark`` on both the agent and the graph surface) create the batch's ``ToolCallTask`` rows. A
``TurnInvariantError`` raised there (``primer.session.persistence``: a row of another session under the id, no durable
TOOL_CALL record, a record_seq mismatch) is raised INSIDE an except handler of the turn's big ``try``, so the sibling
``except Exception`` failure exit never saw it: it propagated out of the turn and the session stayed RUNNING, to hit
the same row the next time the turn ran. The arms now take that same failure exit. A transient storage error is not
an invariant break: it still propagates as itself and the session is not ended.

Mutations, each run red: drop the ``except TurnInvariantError`` of the mixed arm (yield_mixed cases) or of the
tool_wait arm (tool_wait_* cases); make the cross-session guard raise a plain ``RuntimeError`` (every collision case);
widen an arm's catch to ``Exception`` (that arm's transient case); close the turn log before the materialization
again, as it was (the turn-log assertion of that arm).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from primer.int.claim import ClaimKind, Lease
from primer.model.chat import Message, TextPart, ToolCallEnd, ToolCallStart
from primer.model.except_ import ProviderError
from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionMessageKind,
    SessionStatus,
    WorkspaceSession,
)
from primer.model.yield_ import ToolWaitPark, Yielded, YieldToWorker
from primer.observability.turn_log_writer import WorkspaceTurnLogWriter
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn

from tests.conftest import _FakeStorageProvider

_ARMS = ["tool_wait_agent", "tool_wait_graph", "yield_mixed"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _FakeWorkspaceIO:
    def __init__(self) -> None:
        self._data: dict[tuple[str, str], bytes] = {}

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        key = (session_id, "messages.jsonl")
        self._data[key] = self._data.get(key, b"") + line

    def records(self, session_id: str) -> list[dict]:
        raw = self._data.get((session_id, "messages.jsonl"), b"")
        return [json.loads(ln) for ln in raw.decode().splitlines() if ln.strip()]


class _FakeEventBus:
    async def publish(self, key: str, payload: dict) -> None:
        pass


class _RecordingClaimEngine:
    def __init__(self) -> None:
        self.upserted: list[tuple] = []

    async def upsert(self, kind, entity_id: str, **kwargs) -> None:
        self.upserted.append((kind, entity_id))


def _pending_tool_waits() -> list[dict]:
    return [{
        "node_id": "x", "outstanding_task_ids": ["x:tool:0:1"], "notifying_results": [],
        "call_ids": {"x:tool:0:1": "call_a"},
    }]


class _ParkingExecutor:
    """Streams one claimable call (and, for the mixed arm, an ask_user gate) and parks the way ``arm`` names."""

    # A claims batch only exists with the flag on; the TOOL_CALL record stash is gated on it.
    _tool_calls_as_claims_enabled = True

    def __init__(self, arm: str, session_id: str) -> None:
        self._arm = arm
        self._session_id = session_id

    async def invoke(self, messages, **kwargs):
        yield ToolCallStart(id="call_a", name="tool_a", index=0)
        yield ToolCallEnd(id="call_a", arguments={"x": 1}, index=0)
        if self._arm == "yield_mixed":
            yield ToolCallStart(id="call_gate", name="ask_user", index=1)
            yield ToolCallEnd(id="call_gate", arguments={}, index=1)
            park = YieldToWorker(
                Yielded(
                    tool_name="ask_user", event_key=f"ask_user:{self._session_id}:call_gate",
                    resume_metadata={"prompt": "color?"},
                ),
                tool_call_id="call_gate",
                llm_messages=[Message(role="assistant", parts=[TextPart(text="let me check")])],
            )
            park.graph_checkpoint = {"pending_tool_waits": _pending_tool_waits()}
        else:
            park = ToolWaitPark(
                outstanding_task_ids=["x:tool:0:1"], event_key="tool_wait:x:tool:0:1",
                call_ids={"x:tool:0:1": "call_a"},
            )
            if self._arm == "tool_wait_graph":
                park.graph_checkpoint = {"pending_tool_waits": _pending_tool_waits()}
        raise park
        yield  # pragma: no cover - unreachable, keeps this a generator


async def _setup(arm: str):
    storage_provider = _FakeStorageProvider()
    session_id = f"s-invariant-{arm}"
    await storage_provider.get_storage(WorkspaceSession).create(WorkspaceSession(
        id=session_id, workspace_id="w1", binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.RUNNING, created_at=_now(), turn_status="running",
    ))
    io = _FakeWorkspaceIO()
    turn_log_lines: list[bytes] = []

    async def _append_turn_log_line(line: bytes) -> None:
        turn_log_lines.append(line)

    async def _build_executor(_session: WorkspaceSession):
        return _ParkingExecutor(arm, session_id)

    deps = SessionDispatchDeps(
        storage_provider=storage_provider, workspace_io=io, event_bus=_FakeEventBus(),
        build_executor=_build_executor, claim_engine=_RecordingClaimEngine(),
        turn_log_writer_factory=lambda _io, _sid: WorkspaceTurnLogWriter(append_line=_append_turn_log_line),
    )
    lease = Lease(
        kind=ClaimKind.SESSION, entity_id=session_id, claimed_by="worker-1",
        claimed_at=_now(), expires_at=_now(), attempt_count=0, last_error=None,
    )
    return storage_provider, session_id, io, turn_log_lines, deps, lease


@pytest.mark.asyncio
@pytest.mark.parametrize("arm", _ARMS)
async def test_a_cross_session_collision_while_parking_ends_the_session_failed(arm) -> None:
    storage_provider, session_id, io, turn_log_lines, deps, lease = await _setup(arm)
    task_storage = storage_provider.get_storage(ToolCallTask)
    # A row of ANOTHER session under exactly the id this park mints (unreachable by construction, see the guard).
    await task_storage.create(ToolCallTask(
        id=f"{session_id}/x:tool:0:1", session_id="someone-else", turn_no=0, tool_name="tool_a",
        state=ToolCallTaskState.QUEUED, record_seq=1, created_at=_now(),
    ))

    outcome = await run_one_session_turn(lease, deps)

    assert (outcome.success, outcome.drop_lease, outcome.park) == (False, True, None)
    row = await storage_provider.get_storage(WorkspaceSession).get(session_id)
    assert (row.status, row.ended_reason) == (SessionStatus.ENDED, "failed"), "the session was left to run again"
    error = io.records(session_id)[-1]
    assert error["kind"] == SessionMessageKind.ERROR
    assert error["payload"]["title"] == "TurnInvariantError"
    assert "cross-session id collision" in error["payload"]["message"]
    # The turn log says the turn failed, after it said it yielded: it is closed only once the rows exist.
    kinds = [json.loads(line)["kind"] for line in turn_log_lines]
    assert kinds[-1] == "failed" and "yielded" in kinds, kinds
    # The other session's row is never adopted or touched, and no lease was armed for it.
    other = await task_storage.get(f"{session_id}/x:tool:0:1")
    assert other.session_id == "someone-else"
    assert deps.claim_engine.upserted == []


@pytest.mark.asyncio
@pytest.mark.parametrize("arm", _ARMS)
async def test_a_transient_storage_error_while_parking_propagates_and_does_not_end_the_session(
    arm, monkeypatch,
) -> None:
    """Not an invariant break: the error leaves the turn as itself, the session is not ended, and the turn's next
    run parks normally."""
    storage_provider, session_id, io, _lines, deps, lease = await _setup(arm)
    task_storage = storage_provider.get_storage(ToolCallTask)
    real_create = task_storage.create
    calls = 0

    async def _create_failing_once(entity, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ProviderError("storage connection reset")
        return await real_create(entity, **kwargs)

    monkeypatch.setattr(task_storage, "create", _create_failing_once)

    with pytest.raises(ProviderError, match="storage connection reset"):
        await run_one_session_turn(lease, deps)

    row = await storage_provider.get_storage(WorkspaceSession).get(session_id)
    assert (row.status, row.ended_reason) == (SessionStatus.RUNNING, None)
    assert not [r for r in io.records(session_id) if r["kind"] == SessionMessageKind.ERROR]

    outcome = await run_one_session_turn(lease, deps)
    assert outcome.success is True and outcome.park is not None
    assert await task_storage.get(f"{session_id}/x:tool:0:1") is not None
