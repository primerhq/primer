"""Round 4 of the #724 review (security): the agent-session child-graph record lands on the decided sibling, and the no-gate rule is for APPROVALS only (reviewer's JSON ``/var/tmp/review724/r3/final.json``).

* R3-B1 (MEDIUM, audit): on the AGENT-SESSION path through a child graph, ``write_approval_record_for_session`` built the record from the entry the FIRED KEY names, and on a shared key that is the FIRST sibling.
  ``resume_engine_session`` read the wake's gate only AFTER the record write. Root approved ``tool[1]``, only ``danger`` ran, and the record said ``safe`` was approved by root; alice's later rejection of ``safe`` then lost
  the ``gate_event_key`` unique-index race. The REAL writer runs here over SQLite (the unique index applies as on Postgres).
* R3-B2 (MEDIUM, a regression of round 3): the no-gate rule dropped every selected inner-call entry whenever the wake named no gate, WHATEVER the decision. The deadline sweeper publishes a gate-less
  ``__yield_timeout__`` on the park's key: with two sibling gates on it the timeout rejected neither, both re-parked with a fresh deadline (so the gates never expired and could be approved after it) and the timeout's
  record landed on the first sibling, which stayed pending. A gate-less timeout, rejection or cancel rejects every selected entry again (fail closed, as before); only an APPROVAL that cannot be shown to be one
  gate is refused.
* R3-N1: a gate-less decision that selected several gated entries writes no record onto one of them.

The agent-session cases are the production shape: the REAL ``run_invoke_graph`` parks the child, the REAL ``resume_engine_session`` and continuation walk resume it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from primer.graph.invoke_graph import GraphInvocationServices, run_invoke_graph
from primer.model.provider import SqliteConfig
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.model.yield_ import Yielded, YieldToWorker, gate_id_of, with_wake_gate, with_wake_park
from primer.session.yields import durably_mark_session_resumable
from primer.storage.sqlite import SqliteStorageProvider
from primer.worker import session_resume_coordinator as src
from primer.worker.frames import GraphFrame
from primer.worker.yield_runtime import ParkedState
from tests.worker.test_graph_toolcall_inner_gate import _executor
from tests.worker.test_graph_toolcall_inner_gate_round3 import SID, _ChildPool, _pending, _RealWriterPool, _records, _siblings, _SqliteRecords, _two_siblings, _engine

ROOT_DECIDES = {"decision": "approved", "reason": None, "decided_by": "root"}
ALICE_REJECTS = {"decision": "rejected", "reason": "no", "decided_by": "alice"}


async def _sqlite(tmp_path: Path) -> SqliteStorageProvider:
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "records.sqlite"))
    await provider.initialize()
    return provider


class _AgentOverChild:
    """An agent session that invoked a child graph whose two ToolCall siblings are gated on a shared key, resumed round after round with the REAL writer over one SQLite file."""

    def __init__(self, tmp_path: Path, sqlite: SqliteStorageProvider) -> None:
        self.tmp_path = tmp_path
        self.sqlite = sqlite
        self.provider, self.node_manager, self.graph = _siblings()
        self.pool: _ChildPool | None = None

    async def _executor(self, *, graph: Any, gsid: str) -> Any:
        return await _executor(self.tmp_path, self.node_manager, graph)

    async def park(self) -> tuple[Any, dict[str, dict]]:
        async def resolve_graph(_gid: str) -> Any:
            return self.graph

        services = GraphInvocationServices(resolve_graph=resolve_graph, build_child_executor=self._executor, session_id=SID, workspace_id="ws-1", graph_session_id=SID)
        with pytest.raises(YieldToWorker) as first:
            await run_invoke_graph(graph_id="child", graph_input="go", services=services, tool_call_id="outer")
        park = first.value
        assert isinstance(park.frames[-1], GraphFrame) and park.graph_checkpoint is not None
        entries = {e["node_id"]: e for e in park.graph_checkpoint["pending_toolcalls"]}
        assert entries["tool[0]"]["parked_event_key"] == entries["tool[1]"]["parked_event_key"], "the child's siblings share one event key"
        return park, entries

    async def answer(self, yielded: Yielded, frames: list, tool_call_id: str, graph_checkpoint: Any, key: str, wake: dict) -> _ChildPool:
        """Flip the session's park with ``wake`` on ``key`` and resume it. The pool's ``reparked`` is the continuation's re-park."""
        now = datetime.now(timezone.utc) - timedelta(seconds=5)
        stamped = Yielded(tool_name=yielded.tool_name, event_key=yielded.event_key, timeout=yielded.timeout,
                          resume_metadata={**yielded.resume_metadata, "parked_at_iso": now.isoformat()}, event_keys=yielded.event_keys)
        blob = ParkedState(yielded=stamped, llm_messages=[], turn_no=0, started_at=now, tool_call_id=tool_call_id, graph_checkpoint=graph_checkpoint, frames=list(frames)).to_jsonable()
        timed_out = "__yield_timeout__" in wake                               # the sweeper's wake is published once the park's deadline has passed, and names the park it read
        if timed_out:
            wake = with_wake_park(wake, now)
        view = _SqliteRecords(self.sqlite)                                    # records: the shared SQLite file; the session row: memory, new each round
        store = view.get_storage(WorkspaceSession)
        row = WorkspaceSession(
            id=SID, workspace_id="ws-1", binding=AgentSessionBinding(kind="agent", agent_id="ag-1"), status=SessionStatus.RUNNING, created_at=now, turn_no=0,
            parked_status="parked", parked_event_key=key, parked_event_keys=[key], parked_until=now - timedelta(seconds=1) if timed_out else now + timedelta(seconds=600), parked_at=now, parked_state=blob,
        )
        await store.create(row)
        assert await durably_mark_session_resumable(row, event_key=key, payload=wake, session_storage=store, engine=None)

        self.pool = _ChildPool(_ViewAsProvider(view), self._executor, self.graph)
        await src.resume_engine_session(self.pool, None, await store.get(SID))  # type: ignore[arg-type]
        return self.pool


class _ViewAsProvider:
    """``_Pool`` wants ``get_storage``: the view already has it."""

    def __init__(self, view: _SqliteRecords) -> None:
        self._view = view

    def get_storage(self, model_cls: Any) -> Any:
        return self._view.get_storage(model_cls)


# ---- R3-B1 ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_record_of_a_decision_on_the_second_child_sibling_lands_on_that_sibling_and_the_first_siblings_own_decision_keeps_its_record(tmp_path: Path) -> None:
    """Root is admitted to ``danger`` only and approves ``tool[1]``: only ``danger`` runs. The record must be on ``danger``'s gate. Alice then REJECTS ``safe`` and her record is hers: it lost the unique-index race
    to the 'approved by root' that had been written onto ``safe`` while it was still pending."""
    sqlite = await _sqlite(tmp_path)
    try:
        world = _AgentOverChild(tmp_path, sqlite)
        park, entries = await world.park()
        safe, danger = entries["tool[0]"], entries["tool[1]"]
        key = safe["parked_event_key"]
        safe_gate, danger_gate = gate_id_of(safe["resume_metadata"]), gate_id_of(danger["resume_metadata"])

        pool = await world.answer(park.yielded, park.frames, park.tool_call_id, park.graph_checkpoint, key, with_wake_gate(ROOT_DECIDES, danger_gate))
        assert world.provider.runs == ["danger"] and pool.reparked is not None
        assert await _records(_SqliteRecords(sqlite)) == {f"{key}@{danger_gate}": ("approved", "root")}, "the record of a decision on the second child sibling is on the second child sibling"

        again = pool.reparked                                                 # safe is still pending: alice rejects it
        await world.answer(again.leaf, again.frames, park.tool_call_id, None, key, with_wake_gate(ALICE_REJECTS, safe_gate))
        assert world.provider.runs == ["danger"], "the rejected sibling ran"
        assert await _records(_SqliteRecords(sqlite)) == {f"{key}@{danger_gate}": ("approved", "root"), f"{key}@{safe_gate}": ("rejected", "alice")}, "safe's own decision has its own record"
    finally:
        await sqlite.aclose()


@pytest.mark.asyncio
async def test_the_record_of_a_decision_on_the_first_child_sibling_is_still_on_the_first(tmp_path: Path) -> None:
    sqlite = await _sqlite(tmp_path)
    try:
        world = _AgentOverChild(tmp_path, sqlite)
        park, entries = await world.park()
        safe = entries["tool[0]"]
        key, gate = safe["parked_event_key"], gate_id_of(safe["resume_metadata"])
        await world.answer(park.yielded, park.frames, park.tool_call_id, park.graph_checkpoint, key, with_wake_gate({**ROOT_DECIDES, "decided_by": "mallory"}, gate))
        assert world.provider.runs == ["safe"]
        assert await _records(_SqliteRecords(sqlite)) == {f"{key}@{gate}": ("approved", "mallory")}
    finally:
        await sqlite.aclose()


# ---- R3-B2: a gate-less timeout, rejection or cancel ends every gate it selects; a gate-less approval is the only thing refused -----------------------------------------------------------


_GATELESS = {
    "timeout": {"__yield_timeout__": True},
    "cancel": {"__yield_cancelled__": True, "reason": "stop", "cancelled_at": datetime.now(UTC).isoformat()},
    "rejected": {"decision": "rejected", "reason": "no"},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(_GATELESS))
async def test_a_gate_less_timeout_rejection_or_cancel_on_a_shared_key_ends_the_graph_and_leaves_no_gate_pending(tmp_path: Path, kind: str) -> None:
    """The deadline sweeper publishes ``__yield_timeout__`` on the park's primary key and names no gate. With two inner-gate siblings on that key the no-gate rule of round 3 swallowed it: both re-parked with a fresh
    deadline (the gates never expired and could be approved after it). A timeout, a rejection and a cancel reject what they select, as at a43ba8c99: the graph ends, nothing runs, nothing stays parked."""
    world = await _two_siblings(tmp_path)
    sqlite = await _sqlite(tmp_path)
    try:
        storage = _SqliteRecords(sqlite)
        outcome, pool = await _engine(world, tmp_path, {"k": {"event_key": world.key, "payload": _GATELESS[kind]}}, pool_cls=_RealWriterPool, storage=storage)
        assert world.provider.runs == [], f"a gate-less {kind} ran {world.provider.runs}"
        assert pool.repark_calls == [] and str(outcome).startswith("ENDED"), f"a gate-less {kind} left the gates pending ({outcome}, pending {_pending(pool)}): they never expire"
        assert len(pool.end_session_calls) == 1
    finally:
        await sqlite.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(_GATELESS))
async def test_a_gate_less_decision_that_selected_several_gates_writes_no_record_onto_one_of_them(tmp_path: Path, kind: str) -> None:
    """R3-N1 and the record half of R3-B2: the resume-time record of a gate-less decision went onto the FIRST sibling. Either every selected gate gets one or none does, consistently; it is none (one decision cannot be
    shown to be on one gate)."""
    world = await _two_siblings(tmp_path)
    sqlite = await _sqlite(tmp_path)
    try:
        storage = _SqliteRecords(sqlite)
        await _engine(world, tmp_path, {"k": {"event_key": world.key, "payload": _GATELESS[kind]}}, pool_cls=_RealWriterPool, storage=storage)
        assert await _records(storage) == {}, f"a gate-less {kind} wrote a record onto one of two siblings: {await _records(storage)}"
    finally:
        await sqlite.aclose()


@pytest.mark.asyncio
async def test_a_gate_less_approval_that_is_refused_runs_nothing_and_writes_no_record(tmp_path: Path) -> None:
    world = await _two_siblings(tmp_path)
    sqlite = await _sqlite(tmp_path)
    try:
        storage = _SqliteRecords(sqlite)
        outcome, pool = await _engine(world, tmp_path, {"k": {"event_key": world.key, "payload": {"decision": "approved", "decided_by": "mallory"}}}, pool_cls=_RealWriterPool, storage=storage)
        assert world.provider.runs == [] and outcome == "REPARKED" and _pending(pool) == ["tool[0]", "tool[1]"]
        assert await _records(storage) == {}, "'approved by mallory' was written onto a gate nothing ran for"
    finally:
        await sqlite.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(_GATELESS))
async def test_a_gate_less_timeout_rejection_or_cancel_through_a_child_graph_ends_the_child(tmp_path: Path, kind: str) -> None:
    """The same rule through the agent session's walk (``resume_invoke_graph`` is where the decision is known): the child ends failed, nothing runs and it does not re-park."""
    sqlite = await _sqlite(tmp_path)
    try:
        world = _AgentOverChild(tmp_path, sqlite)
        park, entries = await world.park()
        key = entries["tool[0]"]["parked_event_key"]
        pool = await world.answer(park.yielded, park.frames, park.tool_call_id, park.graph_checkpoint, key, _GATELESS[kind])
        assert world.provider.runs == []
        assert pool.reparked is None, f"a gate-less {kind} re-parked the child's gates with a fresh deadline"
    finally:
        await sqlite.aclose()


# ---- the unchanged rule: a gate-less APPROVAL that cannot be shown to be one gate is still refused ----------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_gate_less_approval_through_a_child_graph_is_still_refused(tmp_path: Path) -> None:
    sqlite = await _sqlite(tmp_path)
    try:
        world = _AgentOverChild(tmp_path, sqlite)
        park, entries = await world.park()
        key = entries["tool[0]"]["parked_event_key"]
        pool = await world.answer(park.yielded, park.frames, park.tool_call_id, park.graph_checkpoint, key, {"decision": "approved", "decided_by": "mallory"})
        assert world.provider.runs == [] and pool.reparked is not None
        assert await _records(_SqliteRecords(sqlite)) == {}
    finally:
        await sqlite.aclose()

