"""Round 3 of the #724 review (security): the decided gate reaches a CHILD graph, the audit record lands on the gate that was decided, and what cannot be shown to be that gate stays parked.

Round 2 made a decision run only the inner call of the gate it named (``resumed_gate_id``) for the top-level graph session. Three holes were left (reviewer's JSON ``/var/tmp/review724/r2/final.json``):

* B1-r2a (HIGH): ToolCall siblings inside a CHILD graph (``workspace_ext__invoke_graph`` from an agent session or from a graph agent node) still ran on ONE approval. The decided gate never reached
  ``resume_invoke_graph``: both walks handed it only the classified payload, whose ``__yield_gate_id__`` is stripped. The wake's gate is read RAW now and carried down the walk to the child's resume.
* B1-r2b (MEDIUM): ``write_approval_record_for_graph`` resolved the gate with no ``gate_id``, so on a shared key the record of a decision on the SECOND sibling landed on the FIRST, which is still pending; that
  sibling's own later decision then lost the ``gate_event_key`` unique-index race and the record kept the wrong decider for good. The REAL writer runs here, over SQLite (the shared fakes stub it).
* N1, N2, N5: an entry whose gate id cannot be read is not the gate a wake named; a wake that names no gate runs no inner call when several sibling gates share the key; the ``tool_wait`` wake-only reply
  selects no human gate and writes no record, even if a provider chose the id the old sentinel used.

The shape is the production one: the REAL ``run_invoke_graph`` produces the child's park, the REAL ``resume_engine_session`` and continuation walk resume it, and the siblings are fan-out ToolCall nodes
whose tool runs a gated inner call in a separate manager under one provider-style id (``call_0``), so their inner gates share an event key.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from primer.agent.tool_manager import ToolExecutionManager
from primer.graph.invoke_graph import GraphInvocationServices, resume_invoke_graph, run_invoke_graph
from primer.model.chat import ToolCallPart, ToolCallResult, ToolResultPart
from primer.model.principal import PrincipalRef
from primer.model.provider import SqliteConfig
from primer.model.storage import OffsetPage
from primer.model.tool_approval import ApproverSpec, ToolApprovalRecord
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.model.yield_ import Yielded, YieldToWorker, gate_id_of, with_wake_gate
from primer.session.pending_gates import enumerate_pending_gates
from primer.session.yields import durably_mark_session_resumable
from primer.storage.sqlite import SqliteStorageProvider
from primer.worker import graph_resume_coordinator as grc
from primer.worker import session_resume_coordinator as src
from primer.worker.continuation import resume_continuation
from primer.worker.frames import GraphFrame
from primer.worker.yield_runtime import ParkedState
from tests._resume_hook_fakes import EngineStorageProvider, NullWorkspaceIO, waiting_graph_session
from tests.agent.test_tool_manager_approval_gate import _PoliciesOnlyResolver
from tests.conftest import _FakeStorageProvider
from tests.worker.test_graph_child_sibling_agent_session import _Pool
from tests.worker.test_graph_toolcall_inner_gate import _executor, _fanout_graph, _park_yield, _policy, _SubAgentLike, _ToolCallPool, _workspace_session

SID = "s-agent"
SENTINEL = "__tool_wait_wake_only__"


class _SentinelIds(_SubAgentLike):
    """The same delegating toolset, but the inner call carries the id the tool_wait wake-only reply used as its sentinel (a provider chooses ids; none forbids this one)."""

    async def call(self, *, tool_name: str, arguments: Any, principal: Any = None, ctx: Any = None) -> ToolCallResult:
        if tool_name == "outer":
            tag = (arguments or {}).get("tag")
            result = await self.sub.execute(ToolCallPart(id=SENTINEL, name="_z__safe" if tag == "0" else "_z__danger", arguments={"tag": tag}), principal=principal)
            return ToolCallResult(output=f"outer({result.output})", is_error=result.error)
        return await super().call(tool_name=tool_name, arguments=arguments, principal=principal, ctx=ctx)


def _siblings(provider_cls: type = _SubAgentLike) -> tuple[Any, ToolExecutionManager, Any]:
    """The provider, the NODE's manager and the fan-out graph: ``tool[0]`` asks for ``safe`` (anyone may approve), ``tool[1]`` for ``danger`` (only ``root``)."""
    provider = provider_cls()
    policies = [_policy("_z", "safe"), _policy("_z", "danger", approvers=ApproverSpec(kind="users", users=["root"]))]

    def manager() -> ToolExecutionManager:
        built = ToolExecutionManager(toolset_providers={"_z": provider}, workspace_session=_workspace_session(), initiated_by=PrincipalRef.system())  # type: ignore[dict-item,arg-type]
        built._approval_resolver = _PoliciesOnlyResolver(policies)
        return built

    provider.sub = manager()
    return provider, manager(), _fanout_graph("_z__outer")


@dataclass
class _World:
    provider: Any
    manager: ToolExecutionManager
    graph: Any
    checkpoint: dict
    yld: YieldToWorker
    safe: dict
    danger: dict

    @property
    def key(self) -> str:
        return self.safe["parked_event_key"]

    def gate(self, entry: dict) -> str:
        return gate_id_of(entry["resume_metadata"])


async def _two_siblings(tmp_path: Path, provider_cls: type = _SubAgentLike) -> _World:
    provider, manager, graph = _siblings(provider_cls)
    checkpoint, _seq, yld = await _park_yield(await _executor(tmp_path, manager, graph))
    entries = {e["node_id"]: e for e in checkpoint["pending_toolcalls"]}
    world = _World(provider, manager, graph, checkpoint, yld, entries["tool[0]"], entries["tool[1]"])
    assert world.safe["parked_event_key"] == world.danger["parked_event_key"], "the siblings share one event key"
    assert world.gate(world.safe) != world.gate(world.danger)
    return world


async def _engine(world: _World, tmp_path: Path, payloads: dict, *, pool_cls: type = _ToolCallPool, storage: Any = None, checkpoint: dict | None = None, yld: Any = None) -> tuple[Any, Any]:
    """The real ``resume_graph_engine`` over a pool slice: the outcome and the pool."""
    async def factory() -> Any:
        return await _executor(tmp_path, world.manager, world.graph)

    pool = pool_cls(storage=storage or EngineStorageProvider(), workspace_io=NullWorkspaceIO(), executor_factory=factory)
    session = waiting_graph_session()
    session.parked_state = {"resume_event_payloads": payloads}
    yld = yld or world.yld
    parked = ParkedState(
        yielded=yld.yielded, llm_messages=[], turn_no=0, started_at=datetime.now(UTC), tool_call_id=yld.tool_call_id,
        resume_event_payload=next(iter(payloads.values()))["payload"], graph_checkpoint=checkpoint or world.checkpoint,
    )
    return await grc.resume_graph_engine(pool, session, parked), pool


def _pending(pool: Any) -> list[str]:
    return [e["node_id"] for e in pool.repark_calls[0].graph_checkpoint["pending_toolcalls"]] if pool.repark_calls else []


# ---- B1-r2a: ToolCall siblings inside a CHILD graph, resumed by the REAL walk of an agent session -------------------------------------------------------------------------------------------


class _ChildPool(_Pool):
    """The pool slice ``resume_engine_session`` uses (the shared agent-session one), with the child graph's executor built from this module's world."""

    def __init__(self, sp: Any, build_child: Any, graph: Any) -> None:
        super().__init__(sp)
        self._build_child = build_child
        self._graph = graph

    async def _build_agent_executor(self, session: Any, workspace: Any) -> Any:
        async def resolve_graph(_gid: str) -> Any:
            return self._graph

        return SimpleNamespace(_tool_manager=SimpleNamespace(_graph_services=SimpleNamespace(resolve_graph=resolve_graph, build_child_executor=self._build_child)))


@pytest.mark.asyncio
@pytest.mark.parametrize("decided", ["tool[0]", "tool[1]"], ids=["decide-the-safe-gate", "decide-the-dangerous-gate"])
async def test_a_decision_in_a_child_graph_runs_only_the_inner_call_of_the_gate_it_named(tmp_path: Path, decided: str) -> None:
    """B1-r2a: the agent session parks on the child's primary and carries the whole child checkpoint; the wake names ONE sibling's gate. The walk handed the child's resume the classified payload only, so the
    child selected every entry on the key and each ran its own ``original_call`` with the approval bypassed: mallory, who may decide ``safe`` only, ran ``danger``. At the merge base the same decision ran nothing."""
    provider, pool, _mine, _checkpoint = await _agent_session_over_the_child(tmp_path, decided)
    other = "tool[1]" if decided == "tool[0]" else "tool[0]"
    assert provider.runs == ["safe" if decided == "tool[0]" else "danger"], f"one approval of {decided} ran {provider.runs}: a sibling's unapproved call ran with it"
    assert pool.reparked is not None, "the sibling the decision did not name stays parked in the child"
    assert [e["node_id"] for e in pool.reparked.frames[-1].checkpoint["pending_toolcalls"]] == [other]


async def _agent_session_over_the_child(tmp_path: Path, decided: str, *, name_the_gate: bool = True) -> tuple[Any, _ChildPool, Any, dict]:
    """An agent session that invoked a child graph whose two ToolCall siblings are gated on a shared key; the reply to ``decided`` is flipped in on that key.

    The gate a reply names is only known once the park exists: park, read the minted ids, then answer. Returns the provider, the pool (``reparked`` is the continuation's re-park), the entry the reply is
    for and the child's checkpoint at the park."""
    provider, node_manager, graph = _siblings()

    async def resolve_graph(_gid: str) -> Any:
        return graph

    async def build_child(*, graph: Any, gsid: str) -> Any:
        return await _executor(tmp_path, node_manager, graph)

    services = GraphInvocationServices(resolve_graph=resolve_graph, build_child_executor=build_child, session_id=SID, workspace_id="ws-1", graph_session_id=SID)
    with pytest.raises(YieldToWorker) as first:
        await run_invoke_graph(graph_id="child", graph_input="go", services=services, tool_call_id="outer")
    park = first.value
    assert isinstance(park.frames[-1], GraphFrame) and park.graph_checkpoint is not None
    entries = {e["node_id"]: e for e in park.graph_checkpoint["pending_toolcalls"]}
    mine = entries[decided]
    key = mine["parked_event_key"]
    assert key == next(e for n, e in entries.items() if n != decided)["parked_event_key"], "the child's siblings share one event key"
    payload = {"decision": "approved", "reason": None, "decided_by": "mallory"}
    if name_the_gate:
        payload = with_wake_gate(payload, gate_id_of(mine["resume_metadata"]))
    now = datetime.now(timezone.utc) - timedelta(seconds=5)
    yielded = Yielded(tool_name=park.yielded.tool_name, event_key=park.yielded.event_key, timeout=park.yielded.timeout,
                      resume_metadata={**park.yielded.resume_metadata, "parked_at_iso": now.isoformat()}, event_keys=park.yielded.event_keys)
    blob = ParkedState(yielded=yielded, llm_messages=[], turn_no=0, started_at=now, tool_call_id=park.tool_call_id, graph_checkpoint=park.graph_checkpoint, frames=list(park.frames)).to_jsonable()
    assert [g["node_id"] for g in enumerate_pending_gates(blob)] == ["tool[0]", "tool[1]"], "the pending list offers both siblings"
    sp = _FakeStorageProvider()
    store = sp.get_storage(WorkspaceSession)
    row = WorkspaceSession(
        id=SID, workspace_id="ws-1", binding=AgentSessionBinding(kind="agent", agent_id="ag-1"), status=SessionStatus.RUNNING, created_at=now, turn_no=0,
        parked_status="parked", parked_event_key=key, parked_event_keys=[key], parked_until=now + timedelta(seconds=600), parked_at=now, parked_state=blob,
    )
    await store.create(row)
    assert await durably_mark_session_resumable(row, event_key=key, payload=payload, session_storage=store, engine=None)
    pool = _ChildPool(sp, build_child, graph)
    await src.resume_engine_session(pool, None, await store.get(SID))  # type: ignore[arg-type]
    return provider, pool, mine, park.graph_checkpoint


@pytest.mark.asyncio
@pytest.mark.parametrize("decided", ["tool[0]", "tool[1]"], ids=["decide-the-safe-gate", "decide-the-dangerous-gate"])
async def test_an_approval_that_names_no_gate_runs_no_inner_call_of_the_siblings_in_a_child_graph(tmp_path: Path, decided: str) -> None:
    """N2-r2 through the child: a wake from before gates carried ids (or a channel inbox with no storage provider) names no gate. Several sibling gates share the key, so the decision cannot be shown to
    be any one of them: nothing runs, and both stay parked."""
    provider, pool, _mine, _ck = await _agent_session_over_the_child(tmp_path, decided, name_the_gate=False)
    assert provider.runs == [], f"an approval that named no gate ran {provider.runs}"
    assert pool.reparked is not None
    assert [e["node_id"] for e in pool.reparked.frames[-1].checkpoint["pending_toolcalls"]] == ["tool[0]", "tool[1]"]


# ---- the plumbing the walk is built from, pinned where the walk's own test cannot see a drop -------------------------------------------------------------------------------------------


class _RecordingChild:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    async def resume_from_checkpoint(self, checkpoint: dict, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        if False:
            yield None


@pytest.mark.asyncio
async def test_resume_invoke_graph_hands_the_decided_gate_to_the_child() -> None:
    child = _RecordingChild()
    await resume_invoke_graph(child=child, checkpoint={}, payload={"decision": "approved"}, resumed_tcid="t", resumed_event_key="k", resumed_gate_id="g" * 32)
    assert child.kwargs["resumed_gate_id"] == "g" * 32 and child.kwargs["resumed_event_key"] == "k"


@pytest.mark.asyncio
async def test_graph_frame_resume_leaf_hands_the_decided_gate_to_resume_invoke_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def fake_resume_invoke_graph(**kwargs: Any) -> tuple[str, None]:
        seen.update(kwargs)
        return "out", None

    async def resolve_graph(_gid: str) -> Any:
        return None

    async def build_child(*_a: Any) -> Any:
        return object()

    async def agent_tool_result(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr("primer.worker.frames.resume_invoke_graph", fake_resume_invoke_graph)
    services = SimpleNamespace(resolve_graph=resolve_graph, build_child_graph_executor=build_child, graph_agent_tool_result=agent_tool_result, session_id="s", resolve_provider=None)
    frame = GraphFrame(graph_id="g", gsid="gs", checkpoint={}, tool_call_id="outer", node_tcid="n")
    await frame.resume_leaf(SimpleNamespace(event_key=None), {"decision": "approved"}, services, fired_key=None, gate_id="g" * 32)
    assert seen["resumed_gate_id"] == "g" * 32


@pytest.mark.asyncio
async def test_resume_continuation_hands_the_decided_gate_to_the_innermost_frame() -> None:
    seen: dict[str, Any] = {}

    class _Frame:
        async def resume_leaf(self, leaf: Any, payload: Any, services: Any, fired_key: Any = None, gate_id: Any = None) -> Any:
            seen.update(fired_key=fired_key, gate_id=gate_id)
            return SimpleNamespace(completed=True, value="done")

    await resume_continuation([_Frame()], object(), {}, object(), fired_key="k", gate_id="g" * 32)  # type: ignore[arg-type]
    assert seen == {"fired_key": "k", "gate_id": "g" * 32}


@pytest.mark.asyncio
async def test_the_graph_nested_branch_hands_the_decided_gate_to_the_continuation(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def fake_resume_continuation(frames: Any, leaf: Any, payload: Any, services: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return SimpleNamespace(tool_result=ToolResultPart(id="outer", output="done"))

    monkeypatch.setattr("primer.worker.continuation.resume_continuation", fake_resume_continuation)
    monkeypatch.setattr("primer.worker.frames.frames_from_jsonable", lambda frames: [object()])
    monkeypatch.setattr("primer.model.yield_.Yielded.from_jsonable", classmethod(lambda cls, data: object()))
    pool = SimpleNamespace(_build_invocation_services=lambda *a, **k: object())
    await grc.resume_graph_continuation(pool, object(), object(), {}, {"frames": [{}], "leaf": {}}, {"decision": "approved"}, object(), object(), gate_id="g" * 32)  # type: ignore[arg-type]
    assert seen.get("gate_id") == "g" * 32


class _NestedPool(_ToolCallPool):
    """A pool whose node is parked on a nested continuation: it records what the engine hands the walk and stops there."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.continuation_kwargs: dict[str, Any] | None = None

    def _graph_nested_agent_yield(self, checkpoint: dict, tcid: Any, event_key: Any = None) -> dict:
        return {"frames": [{}], "leaf": {}}

    async def _resume_graph_continuation(self, session: Any, parked: Any, checkpoint: dict, ay: Any, payload: Any, workspace: Any, executor: Any, **kwargs: Any) -> Any:
        self.continuation_kwargs = kwargs
        return SimpleNamespace(repark_outcome="STOPPED", agent_tool_result=None)


@pytest.mark.asyncio
async def test_resume_graph_engine_hands_the_reply_gate_to_a_nested_continuation(tmp_path: Path) -> None:
    world = await _two_siblings(tmp_path)
    gate = world.gate(world.danger)
    payload = with_wake_gate({"decision": "approved", "decided_by": "root"}, gate)
    outcome, pool = await _engine(world, tmp_path, {"k": {"event_key": world.key, "payload": payload}}, pool_cls=_NestedPool)
    assert outcome == "STOPPED"
    assert (pool.continuation_kwargs or {}).get("gate_id") == gate


# ---- B1-r2b: the approval record lands on the gate that was decided (the REAL writer over SQLite) -------------------------------------------------------------------------------------------


class _SqliteRecords:
    """A storage provider whose ToolApprovalRecord storage is real SQLite (the unique index on ``gate_event_key`` applies, as on Postgres); every other model is in memory."""

    def __init__(self, sqlite: SqliteStorageProvider) -> None:
        self._sqlite = sqlite
        self.aclose = sqlite.aclose
        self._memory = EngineStorageProvider()

    def get_storage(self, model_cls: Any) -> Any:
        return self._sqlite.get_storage(model_cls) if model_cls is ToolApprovalRecord else self._memory.get_storage(model_cls)


class _RealWriterPool(_ToolCallPool):
    """``resume_graph_engine``'s pool with the REAL approval-record writer: whatever the coordinator passes is forwarded (the shared fakes stub the writer and record nothing)."""

    async def _write_approval_record_for_graph(self, **kwargs: Any) -> None:
        self.approval_record_event_keys.append(kwargs.get("event_key"))
        await grc.write_approval_record_for_graph(self, **kwargs)


async def _records(storage: _SqliteRecords) -> dict[str, tuple[str, Any]]:
    page = await storage.get_storage(ToolApprovalRecord).list(OffsetPage(offset=0, length=200))
    return {r.gate_event_key: (r.decision, r.decided_by) for r in page.items}


@pytest.mark.asyncio
async def test_the_record_of_a_decision_on_the_second_sibling_lands_on_that_sibling_and_the_first_siblings_own_decision_keeps_its_record(tmp_path: Path) -> None:
    """B1-r2b: ``resolve_pending_gate`` without ``gate_id`` on a shared key resolves the FIRST entry, which is still pending (the decision of the second re-parks it). The record said the first gate was approved by bob;
    the first gate's real decision (rejected by alice) then lost the unique-index race and the record kept bob's name for ever."""
    world = await _two_siblings(tmp_path)
    storage = _SqliteRecords(SqliteStorageProvider(SqliteConfig(path=tmp_path / "records.sqlite")))
    await storage._sqlite.initialize()
    try:
        danger_gate, safe_gate = world.gate(world.danger), world.gate(world.safe)
        approved = with_wake_gate({"decision": "approved", "reason": None, "decided_by": "root"}, danger_gate)
        outcome, pool = await _engine(world, tmp_path, {"k": {"event_key": world.key, "payload": approved}}, pool_cls=_RealWriterPool, storage=storage)
        assert outcome == "REPARKED" and world.provider.runs == ["danger"]
        assert await _records(storage) == {f"{world.key}@{danger_gate}": ("approved", "root")}, "the record of a decision on the second sibling is on the second sibling"

        rejected = with_wake_gate({"decision": "rejected", "reason": "no", "decided_by": "alice"}, safe_gate)
        repark = pool.repark_calls[0]
        outcome2, _pool2 = await _engine(world, tmp_path, {"k": {"event_key": world.key, "payload": rejected}}, pool_cls=_RealWriterPool, storage=storage, checkpoint=repark.graph_checkpoint, yld=repark)
        assert world.provider.runs == ["danger"], "the rejected sibling ran"
        assert await _records(storage) == {f"{world.key}@{danger_gate}": ("approved", "root"), f"{world.key}@{safe_gate}": ("rejected", "alice")}, "the first sibling's own decision has its own record"
        del outcome2
    finally:
        await storage.aclose()


# ---- N1, N2: what cannot be shown to be the gate a wake named stays parked, and a wake that names no gate runs no inner call when siblings share the key ------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["missing", "empty", "int", "none"])
async def test_a_sibling_whose_gate_id_cannot_be_read_stays_parked_when_the_wake_names_a_gate(tmp_path: Path, shape: str) -> None:
    """N1-r2: ``in (resumed_gate_id, None)`` kept the entry with no readable gate id, so a decision that named gate A also ran that sibling's inner call. Every gate mints an id today; a corrupted park, or one
    that straddles an upgrade, is the only way to meet one, and an entry that cannot be shown to be the gate decided is not run."""
    world = await _two_siblings(tmp_path)
    checkpoint = copy.deepcopy(world.checkpoint)
    other = next(e for e in checkpoint["pending_toolcalls"] if e["node_id"] == "tool[1]")
    if shape == "missing":
        other["resume_metadata"].pop("gate_id", None)
    else:
        other["resume_metadata"]["gate_id"] = {"empty": "", "int": 12345, "none": None}[shape]
    payload = with_wake_gate({"decision": "approved", "decided_by": "mallory"}, world.gate(world.safe))
    outcome, pool = await _engine(world, tmp_path, {"k": {"event_key": world.key, "payload": payload}}, checkpoint=checkpoint)
    assert world.provider.runs == ["safe"], f"a decision that named gate A ran the sibling with {shape} gate id too: {world.provider.runs}"
    assert outcome == "REPARKED" and _pending(pool) == ["tool[1]"]


@pytest.mark.asyncio
async def test_an_approval_that_names_no_gate_runs_no_inner_call_when_siblings_with_different_gates_share_the_key(tmp_path: Path) -> None:
    """N2-r2: an approval wake that names no gate (a wake from before gates had ids, the channel inbox with no storage provider, the key-less legacy drain) ran every inner call on the key; at the merge base
    nothing ran. One decision cannot be shown to be several gates: nothing runs and both stay parked."""
    world = await _two_siblings(tmp_path)
    payload = {"decision": "approved", "decided_by": "mallory"}
    outcome, pool = await _engine(world, tmp_path, {"k": {"event_key": world.key, "payload": payload}})
    assert world.provider.runs == [], f"an approval that named no gate ran {world.provider.runs}"
    assert outcome == "REPARKED" and _pending(pool) == ["tool[0]", "tool[1]"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["rejected", "timeout", "cancel"])
async def test_a_rejection_a_timeout_and_a_cancel_that_name_no_gate_still_run_nothing(tmp_path: Path, kind: str) -> None:
    """The controls: they ran nothing before this round and run nothing now."""
    world = await _two_siblings(tmp_path)
    payload = {
        "rejected": {"decision": "rejected", "reason": "no"},
        "timeout": {"__yield_timeout__": True},
        "cancel": {"__yield_cancelled__": True, "reason": "stop", "cancelled_at": datetime.now(UTC).isoformat()},
    }[kind]
    await _engine(world, tmp_path, {"k": {"event_key": world.key, "payload": payload}})
    assert world.provider.runs == []


# ---- N5: the tool_wait wake-only reply selects no human gate and writes no record ------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_tool_wait_wake_that_decides_nothing_runs_no_gated_call_even_when_a_provider_chose_the_sentinel_id(tmp_path: Path) -> None:
    """N5-r2: with only a co-pending tool_wait batch's wake the engine replies with a sentinel tool_call_id and an "approved" decision. A pending approval whose RAW id is the sentinel (a provider chooses
    ids; the claims flag has to be on for a tool_wait park to exist) was selected by that id, and its gated inner call ran with no human decision; the record said approved, decided by nobody."""
    world = await _two_siblings(tmp_path, _SentinelIds)
    assert {e["tool_call_id"] for e in world.checkpoint["pending_toolcalls"]} == {SENTINEL}, "the inner gates carry the sentinel as their raw id"
    storage = _SqliteRecords(SqliteStorageProvider(SqliteConfig(path=tmp_path / "records.sqlite")))
    await storage._sqlite.initialize()
    try:
        payloads = {"tw": {"event_key": "tool_wait:s:1:other", "payload": {"tool_wait_ready": True}}}
        outcome, pool = await _engine(world, tmp_path, payloads, pool_cls=_RealWriterPool, storage=storage)
        assert world.provider.runs == [], f"a tool_wait wake with no human decision ran {world.provider.runs}"
        assert outcome == "REPARKED" and _pending(pool) == ["tool[0]", "tool[1]"]
        assert await _records(storage) == {}, "a record was written for a decision nobody made"
        assert pool.approval_record_event_keys == []
    finally:
        await storage.aclose()
