"""A graph parked on several approval gates, answered in every order, through the real worker (GR-3, Task 103).

The graph is ``begin -> a -> enda`` and ``begin -> b -> endb``: two ToolCall nodes whose first dispatch parks at an approval gate and whose
approved re-dispatch runs the tool. The session row lives in storage, the replies land through the real durable flip, and each resume runs
through ``WorkerPool._run_engine_session`` with the real session claim adapter on an in-memory claim engine.

Characterization (passes on the code before Task 103 and after it): answers given in order, out of order, or both before the worker claims the
row; a re-park with no reply during the resume writes the park columns it always wrote and leaves no lease.

GR-3: an approval accepted while the worker is still resuming an earlier one must survive the re-park and run on the next claim.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.graph.executor import GraphExecutor
from primer.int.claim import ClaimKind
from primer.model.chat import ToolResultPart
from primer.model.graph import Graph, GraphNodeMessage, GraphThread, _BeginNode, _EndNode, _StaticEdge, _ToolCallNode
from primer.model.scheduler import WorkerConfig
from primer.model.workspace_session import GraphSessionBinding, SessionStatus, WorkspaceSession
from primer.model.yield_ import Yielded, YieldToWorker, new_gate_id, with_wake_gate
from primer.session.yields import durably_mark_session_resumable
from primer.worker.graph_resume_coordinator import _repark_graph_yield_outcome
from primer.worker.pool import WorkerPool
from tests._resume_hook_fakes import NullWorkspaceIO, make_toolcall_executor
from tests.conftest import _FakeStorageProvider
from tests.graph.test_toolcall_dispatch import _InMemoryStorage

SID = "s-multi-gate"
WORKER = "wrk-multi-gate"


def _graph() -> Graph:
    return Graph(
        id="g-two-gates", description="two gated tools in one superstep",
        nodes=[
            _BeginNode(id="begin"),
            _ToolCallNode(id="a", tool_id="x__a", arguments={}),
            _ToolCallNode(id="b", tool_id="x__b", arguments={}),
            _EndNode(id="enda", output_template="a"),
            _EndNode(id="endb", output_template="b"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="a"),
            _StaticEdge(from_node="begin", to_node="b"),
            _StaticEdge(from_node="a", to_node="enda"),
            _StaticEdge(from_node="b", to_node="endb"),
        ],
    )


def _key(node: str) -> str:
    return f"tool_approval:{SID}:{node}:tc-{node}"


class _Tools:
    """Each node's first dispatch parks at its own gate; the approved re-dispatch runs it (``ran``), after ``during[node]`` if one is set."""

    def __init__(self) -> None:
        self.ran: list[str] = []
        self.gates: dict[str, str] = {}
        self.during: dict[str, Any] = {}

    async def __call__(self, node: Any, arguments: Any, bypass_approval: bool = False) -> ToolResultPart:
        if not bypass_approval:
            gate = self.gates.setdefault(node.id, new_gate_id())
            meta = {"policy_id": "pol", "approval_type": "required", "gate_reason": "m", "approvers": None, "gate_id": gate,
                    "original_call": {"id": f"tc-{node.id}", "name": node.tool_id, "arguments": {}}}
            raise YieldToWorker(Yielded(tool_name="_approval", event_key=_key(node.id), resume_metadata=meta), tool_call_id=f"tc-{node.id}")
        if node.id in self.during:
            await self.during.pop(node.id)()
        self.ran.append(node.id)
        return ToolResultPart(id=f"tc-{node.id}", output=f"ran {node.id}")


class _World:
    def __init__(self) -> None:
        self.sp = _FakeStorageProvider()
        self.sessions = self.sp.get_storage(WorkspaceSession)
        self.engine = InMemoryClaimEngine(adapters={ClaimKind.SESSION: SessionClaimAdapter(session_storage=self.sessions)})
        self.tools = _Tools()
        self.graph = _graph()
        self.ts: _InMemoryStorage[GraphThread] = _InMemoryStorage(GraphThread)
        self.ms: _InMemoryStorage[GraphNodeMessage] = _InMemoryStorage(GraphNodeMessage)
        self.thread: Any = None

    def executor(self) -> GraphExecutor:
        return make_toolcall_executor(self.graph, self.thread, self.ts, self.ms, self.tools)

    async def park(self) -> None:
        """Run the graph to its two gates and store the park as the worker writes it."""
        self.thread = await GraphExecutor.open_thread(graph=self.graph, thread_storage=self.ts, title="t")  # type: ignore[arg-type]
        with pytest.raises(YieldToWorker) as ei:
            async for _ev in self.executor().invoke([]):
                pass
        yld = ei.value
        assert sorted(yld.yielded.event_keys or []) == sorted([_key("a"), _key("b")])
        park = _repark_graph_yield_outcome(SimpleNamespace(turn_no=0), yld).park
        await self.sessions.create(WorkspaceSession(
            id=SID, workspace_id="ws-1", binding=GraphSessionBinding(graph_id=self.graph.id), status=SessionStatus.RUNNING,
            created_at=datetime.now(UTC), parked_status="parked", parked_at=park.parked_at, parked_event_key=park.parked_event_key,
            parked_event_keys=park.parked_event_keys, parked_until=park.parked_until, parked_state=park.parked_state,
        ))

    async def approve(self, node: str) -> bool:
        payload = with_wake_gate({"decision": "approved", "reason": None, "decided_by": "op"}, self.tools.gates[node])
        return await durably_mark_session_resumable(
            await self.sessions.get(SID), event_key=_key(node), payload=payload, session_storage=self.sessions, engine=self.engine,
        )

    async def run_worker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Claim the row's lease and run the resume branch, as the worker does."""
        pool = WorkerPool(config=WorkerConfig(concurrency=1), scheduler=None, storage=self.sp, workspace_registry=None,  # type: ignore[arg-type]
                          provider_registry=None, engine=self.engine, event_bus=None)
        pool._worker_id = WORKER

        async def _workspace(_wid: str) -> Any:
            return NullWorkspaceIO()

        async def _build(_s: Any, _w: Any) -> Any:
            return self.executor()

        monkeypatch.setattr(pool, "_load_workspace_for_persist", _workspace)
        monkeypatch.setattr(pool, "_build_graph_executor", _build)
        leases = await self.engine.claim_due(WORKER, max_count=10)
        (lease,) = [ln for ln in leases if ln.kind == ClaimKind.SESSION and ln.entity_id == SID]
        await pool._run_engine_session(lease)

    async def row(self) -> WorkspaceSession:
        row = await self.sessions.get(SID)
        assert row is not None
        return row

    async def has_lease(self) -> bool:
        return await self.engine.has_lease(ClaimKind.SESSION, SID)


def _held_keys(row: WorkspaceSession) -> list[str]:
    return sorted(e.get("event_key") for e in ((row.parked_state or {}).get("resume_event_payloads") or {}).values())


async def _world() -> _World:
    world = _World()
    await world.park()
    return world


# ---- characterization ------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gates_answered_in_order_each_run_on_their_own_claim(monkeypatch) -> None:
    world = await _world()

    assert await world.approve("a")
    await world.run_worker(monkeypatch)
    after_a = await world.row()
    state_a = (after_a.parked_status, after_a.parked_event_key, after_a.parked_event_keys, _held_keys(after_a), await world.has_lease())

    assert await world.approve("b")
    await world.run_worker(monkeypatch)
    after_b = await world.row()

    assert state_a == ("parked", _key("b"), [_key("b")], [], False)
    assert world.tools.ran == ["a", "b"]
    assert (after_b.status, after_b.ended_reason, after_b.parked_status) == (SessionStatus.ENDED, "completed", None)


@pytest.mark.asyncio
async def test_gates_answered_out_of_order_each_run_on_their_own_claim(monkeypatch) -> None:
    world = await _world()

    assert await world.approve("b")
    await world.run_worker(monkeypatch)
    after_b = await world.row()
    state_b = (after_b.parked_status, after_b.parked_event_key, after_b.parked_event_keys, _held_keys(after_b), await world.has_lease())

    assert await world.approve("a")
    await world.run_worker(monkeypatch)

    assert state_b == ("parked", _key("a"), [_key("a")], [], False)
    assert world.tools.ran == ["b", "a"]
    assert (await world.row()).ended_reason == "completed"


@pytest.mark.asyncio
async def test_both_gates_answered_before_the_claim_run_in_one_resume(monkeypatch) -> None:
    world = await _world()

    assert await world.approve("a")
    assert await world.approve("b")
    await world.run_worker(monkeypatch)

    assert sorted(world.tools.ran) == ["a", "b"]
    row = await world.row()
    assert (row.status, row.ended_reason, await world.has_lease()) == (SessionStatus.ENDED, "completed", False)


@pytest.mark.asyncio
async def test_a_repark_with_no_reply_during_the_resume_writes_the_park_it_always_wrote(monkeypatch) -> None:
    world = await _world()
    assert await world.approve("a")
    before = datetime.now(UTC)

    await world.run_worker(monkeypatch)
    row = await world.row()

    assert row.parked_status == "parked"
    assert (row.parked_event_key, row.parked_event_keys) == (_key("b"), [_key("b")])
    assert row.parked_at is not None and row.parked_at >= before
    assert row.parked_until is not None and timedelta(0) < row.parked_until - row.parked_at <= timedelta(seconds=3600)
    state = row.parked_state or {}
    assert [e["node_id"] for e in state["graph_checkpoint"]["pending_toolcalls"]] == ["b"]
    assert state["yielded"]["event_key"] == _key("b")
    assert (state.get("resume_event_payloads"), state.get("resume_event_payload")) == (None, None)
    assert not await world.has_lease()


# ---- GR-3: a reply accepted while the resume runs ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_approval_accepted_during_the_resume_survives_the_repark_and_runs_on_the_next_claim(monkeypatch) -> None:
    world = await _world()
    accepted: dict[str, bool] = {}

    async def _approve_b_while_a_runs() -> None:
        accepted["b"] = await world.approve("b")

    world.tools.during["a"] = _approve_b_while_a_runs
    assert await world.approve("a")
    await world.run_worker(monkeypatch)
    row = await world.row()
    after_a = (row.parked_status, row.parked_event_keys, _held_keys(row), await world.has_lease())

    await world.run_worker(monkeypatch)

    assert accepted == {"b": True}, "B's approval was accepted (the respond route answers 202)"
    assert after_a == ("resumable", [_key("b")], [_key("b")], True)
    assert world.tools.ran == ["a", "b"]
    assert (await world.row()).ended_reason == "completed"
