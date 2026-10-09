"""Concurrent graph siblings that delegate write each call row BEFORE the records of the run it started (ticket 01a11cca, the producer half).

``delegate_node_id`` says which node a delegated run belongs to, but both readers still need the CALL row to nest under, and the row has to be in the log first: ``_stream_agent_node``
only ``queue.put()``s the ``ToolCallEnd`` (the queue is unbounded), the loop dispatched the tool without waiting for the drainer, and ``run_subagent``'s recorder appends straight to
the writer. So with a REAL ``GraphExecutor`` fan-out of two workers that both call a delegating tool under raw id ``call_0`` the log read: both runs' first records, worker 0's call, the
rest of both runs, worker 1's call. The console put everything under worker 0's call and nothing under worker 1's; the timeline left the first records at the root.

A graph node whose executor has a coalesce state bound (session dispatch binds one every turn) now awaits the dispatch barrier before the in-process tool loop too, as the claims path always
did, so the drainer has written its calls before any tool runs. These cases drive the real executor, the real ``run_subagent``, the real ``DelegationRecorder`` and the real
``translate_stream_event`` the way ``dispatch.py`` wires them, and fold the log with both readers.

The same fan-out inside one or two SUBGRAPH nodes (round 3 of #659): the child executor shares the parent's coalesce state, so its agent node awaited a barrier on the CHILD's queue, which
resolved once the child's drainer had forwarded the events to the PARENT's queue, not once session dispatch had written them. Every case below that folds a log runs at depth 0 (flat),
1 and 2 (the number of subgraph nodes around the fan-out).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import primer.graph._agent_node as agent_node
from primer.agent.invoke import build_subagent_toolmanager
from primer.graph._node_identity import current_graph_node_id
from primer.graph.executor import GraphExecutor
from primer.graph.router import RouterRegistry
from primer.model.agent import Agent, AgentModel
from primer.model.chat import Done, TextDelta, ToolCallEnd, ToolCallStart
from primer.model.graph import (
    FanOutSpec,
    Graph,
    GraphNodeMessage,
    GraphThread,
    _AgentNodeRef,
    _BeginNode,
    _EndNode,
    _FanInNode,
    _FanOutNode,
    _GraphNodeRef,
    _StaticEdge,
)
from primer.model.model_profile import ModelProfileConfig
from primer.model.workspace_session import SessionMessageKind, SessionMessageRecord
from primer.model_profile import ResolvedModel
from primer.session.delegation import DelegationRecorder, reset_delegation_sink, set_delegation_sink
from primer.session.persistence import _CoalesceState, translate_stream_event
from primer.session.timeline import build_turn_timeline
from primer.worker.frames import AgentResumeContext
from tests.agent.test_delegated_runs_carry_a_run_id import _text, _world
from tests.graph.test_fanout_broadcast_e2e import _InMemoryStorage
from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
T = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)


class _Log:
    """What the dispatch's writer and the recorder append to: one ordered log (a yield between appends lets concurrent writers interleave)."""

    def __init__(self) -> None:
        self.records: list[SessionMessageRecord] = []

    def add(self, rec: SessionMessageRecord) -> int:
        seq = len(self.records) + 1
        self.records.append(rec.model_copy(update={"seq": seq, "created_at": T + timedelta(seconds=seq)}))
        return seq

    async def append(self, rec: SessionMessageRecord) -> int:
        await asyncio.sleep(0)
        return self.add(rec)


class _Bus:
    async def publish(self, key, payload) -> None:
        return None


class _WorkerLLM:
    """Asks for the delegating tool under the raw id ``call_0`` (every worker does: providers that number per stream), then answers."""

    async def list_models(self):
        return ["m"]

    def stream(self, *, model, messages, **kw: Any):
        answered = any(m.role == "tool" for m in messages)

        async def gen():
            await asyncio.sleep(0)
            if not answered:
                yield ToolCallStart(id="call_0", name="t1__delegate", index=0)
                await asyncio.sleep(0)
                yield ToolCallEnd(id="call_0", arguments={}, index=0)
                yield Done(stop_reason="tool_use", raw_reason="tool_use")
            else:
                yield TextDelta(text="worker done", index=0)
                yield Done(stop_reason="stop", raw_reason="stop")

        return gen()


def _fanout_graph(workers: int = 2) -> Graph:
    return Graph.model_construct(
        id="g", description="fan-out delegation",
        nodes=[
            _BeginNode(id="begin"),
            _FanOutNode(id="fan", specs=[FanOutSpec(kind="broadcast", target_node_id="worker", count=workers)]),
            _AgentNodeRef(id="worker", agent_id="ag", input_template="W{{ fanout_index }}"),
            _FanInNode(id="agg", aggregate_template="{% for n in nodes.worker %}{{ n.text }}{% endfor %}"),
            _EndNode(id="end", output_template="{{ nodes.agg.text }}"),
        ],
        edges=[_StaticEdge(from_node="begin", to_node="fan"), _StaticEdge(from_node="worker", to_node="agg"), _StaticEdge(from_node="agg", to_node="end")],
        max_iterations=10, harness_id=None,
    )


def _wrapper_graph(graph_id: str, inner_id: str) -> Graph:
    """begin -> one subgraph node running ``inner_id`` -> end."""
    return Graph.model_construct(
        id=graph_id, description=f"a subgraph node that runs {inner_id}",
        nodes=[_BeginNode(id="begin"), _GraphNodeRef(id="sub", graph_id=inner_id), _EndNode(id="end", output_template="{{ nodes.sub.text }}")],
        edges=[_StaticEdge(from_node="begin", to_node="sub"), _StaticEdge(from_node="sub", to_node="end")],
        max_iterations=10, harness_id=None,
    )


async def _executor(llm: Any, *, workers: int = 2, answers: int = 2, depth: int = 0, monkeypatch: pytest.MonkeyPatch | None = None) -> GraphExecutor:
    """The fan-out graph, ``depth`` subgraph nodes deep (0: it is the executor's own graph).

    A child is built by ``GraphExecutor._build_sub_executor``, which does not carry the coalesce state; ``WorkspaceGraphExecutor._build_sub_executor`` (what production runs) does
    (``tests/graph/test_workspace_executor.py::test_build_sub_executor_inherits_coalesce_state``), so a nested scene patches the base the same way."""
    storage, registry = _world([_text(f"sub answer {i}") for i in range(answers)])
    ctx = AgentResumeContext(session_id="s", workspace_id="ws-1", chat_id=None, principal="user-1", tools=["t1__delegate"], turn_no=1)

    async def tm_resolver(agent):
        return await build_subagent_toolmanager(ctx, storage_provider=storage, provider_registry=registry)

    async def agent_resolver(agent_id):
        return Agent(id=agent_id, description="x", model=AgentModel(profile_id="p--m"), system_prompt=[])

    async def llm_resolver(agent, *a, **k):
        return (llm, ResolvedModel(profile_id="p", provider_id="pv", model_name="m", context_length=128_000, config=ModelProfileConfig()))

    graphs = {"g": _fanout_graph(workers)}
    top = "g"
    for level in range(1, depth + 1):
        graphs[f"level{level}"] = _wrapper_graph(f"level{level}", top)
        top = f"level{level}"

    async def graph_resolver(graph_id):
        return graphs[graph_id]

    if depth:
        assert monkeypatch is not None, "a nested scene patches the sub-executor builder"
        real_build = GraphExecutor._build_sub_executor

        async def build_bound(self, *a, **k):
            child = await real_build(self, *a, **k)
            child.bind_coalesce_state(self._coalesce_state)
            return child

        monkeypatch.setattr(GraphExecutor, "_build_sub_executor", build_bound)

    ts, ms = _InMemoryStorage(GraphThread), _InMemoryStorage(GraphNodeMessage)
    graph = graphs[top]
    thread = await GraphExecutor.open_thread(graph=graph, thread_storage=ts, title="t")
    return GraphExecutor(
        graph=graph, agent_resolver=agent_resolver, llm_resolver=llm_resolver, thread_storage=ts, message_storage=ms,
        graph_thread_id=thread.id, router_registry=RouterRegistry(), tool_manager_resolver=tm_resolver, graph_resolver=graph_resolver,
    )


async def _run(ex: GraphExecutor, *, bind_state: bool = True, log: _Log | None = None) -> list[dict]:
    """The records session dispatch writes for this executor: its events through ``translate_stream_event`` with the coalesce state bound to the executor (``dispatch.py``).

    ``log`` is where they are written; a test that expects the stream to raise passes its own to read what was written before it did."""
    log, state = log if log is not None else _Log(), _CoalesceState()
    if bind_state:
        ex.bind_coalesce_state(state)
    log.add(SessionMessageRecord(seq=1, kind=SessionMessageKind.USER_INPUT, payload={"text": "go"}, created_at=T))
    token = set_delegation_sink(DelegationRecorder(writer=log, event_bus=_Bus(), session_id="s", turn_no=1))
    try:
        async for ev in ex.invoke([]):
            for rec in _as_list(translate_stream_event(ev, state, turn_no=1)):
                await log.append(rec)
    finally:
        reset_delegation_sink(token)
    for rec in _as_list(translate_stream_event(Done(stop_reason="stop", raw_reason="stop"), state, turn_no=1)):
        log.add(rec)
    return [r.model_dump(mode="json") for r in log.records]


def _as_list(result) -> list:
    return [] if result is None else (result if isinstance(result, list) else [result])


def _calls(records: list[dict]) -> dict[str, dict]:
    """The graph nodes' own ``invoke_agent``-style call rows, by the node that made them."""
    return {r["node_id"]: r for r in records if r["kind"] == "tool_call" and not r["payload"].get("delegated") and r.get("node_id")}


def _delegated(records: list[dict]) -> list[dict]:
    return [r for r in records if r["payload"].get("delegated")]


_JS: Any = None


def _js_children(records: list[dict]) -> dict[int, list[int]]:
    """The console's nesting (``SH_nestWithResults`` over ``SA_toTranscript``): call seq -> the seqs of its children."""
    ui = ROOT / "ui"
    prelude = "\n".join([
        (ui / "foundation" / "shell-status.js").read_text(encoding="utf-8"),
        transpile(ui / "components" / "session-adapter.jsx"),
        (ui / "foundation" / "shell-turns.js").read_text(encoding="utf-8"),
    ])
    ctx = mini_react_context("", prelude)
    try:
        out = ctx.eval(
            "(function () { var n = SH_nestWithResults(SA_toTranscript(" + json.dumps(records) + ", null)); var out = {};"
            " function walk(rows) { rows.forEach(function (r) { if (r.kind === 'tool_call') out[r.seq] = (r.children || []).map(function (k) { return k.seq; }); walk(r.children || []); }); }"
            " walk(n.flat); return JSON.stringify(out); })()"
        )
        return {int(k): v for k, v in json.loads(out).items()}
    finally:
        ctx.close()


_TIMELINE_KINDS = ("llm_call", "tool_call")   # what the timeline folds into its tree; the console also draws a run's text and its end


def _py_children(records: list[dict]) -> dict[int, list[int]]:
    """The timeline's nesting: call seq -> the seqs of its children."""
    tl = build_turn_timeline(message_lines=[json.dumps(r) for r in records], turn_log_lines=[], turn_no=0)
    out: dict[int, list[int]] = {}

    def walk(node: dict) -> None:
        for child in node.get("children", []):
            if child["kind"] == "tool_call":
                out[child["seq"]] = [k["seq"] for k in child["children"]]
            walk(child)

    walk(tl)
    return out


@pytest.fixture(params=[0, 1, 2], ids=lambda d: f"depth{d}")
def depth(request: pytest.FixtureRequest) -> int:
    """How many subgraph nodes the fan-out sits inside."""
    return request.param


async def _records(depth: int, monkeypatch: pytest.MonkeyPatch, **kw: Any) -> list[dict]:
    return await _run(await _executor(_WorkerLLM(), depth=depth, monkeypatch=monkeypatch, **kw))


async def test_the_scene_is_two_workers_that_each_delegate_under_one_raw_id(depth: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """The control: the scene really is two instances, each with its own call row and its own run, all under ``call_0``."""
    records = await _records(depth, monkeypatch)
    calls = _calls(records)
    assert sorted(calls) == ["worker[0]", "worker[1]"], sorted(calls)
    assert {c["payload"]["raw_id"] for c in calls.values()} == {"call_0"}
    runs = {r["payload"]["delegate_run_id"] for r in _delegated(records)}
    assert len(runs) == 2 and {r["payload"]["delegate_node_id"] for r in _delegated(records)} == {"worker[0]", "worker[1]"}


async def test_every_delegated_record_is_written_after_the_call_row_that_started_it(depth: int, monkeypatch: pytest.MonkeyPatch) -> None:
    records = await _records(depth, monkeypatch)
    calls = _calls(records)
    late = [
        (r["seq"], r["payload"]["delegate_node_id"], calls[r["payload"]["delegate_node_id"]]["seq"])
        for r in _delegated(records) if r["seq"] < calls[r["payload"]["delegate_node_id"]]["seq"]
    ]
    assert late == [], f"(seq of the delegated record, its node, seq of that node's call row): {late}"


async def test_the_timeline_puts_each_run_under_its_own_nodes_call(depth: int, monkeypatch: pytest.MonkeyPatch) -> None:
    records = await _records(depth, monkeypatch)
    calls, by_seq = _calls(records), {r["seq"]: r for r in records}
    children = _py_children(records)
    for node, call in calls.items():
        mine = children[call["seq"]]
        assert mine, f"{node}: nothing nested under its call row"
        assert {by_seq[s]["payload"].get("delegate_node_id") for s in mine} == {node}, (node, mine)
    folded = {r["seq"] for r in _delegated(records) if r["kind"] in _TIMELINE_KINDS}
    assert folded and {s for kids in children.values() for s in kids} >= folded, "every run's llm_call and tool_call records are under some call"


async def test_the_console_puts_each_run_under_its_own_nodes_call(depth: int, monkeypatch: pytest.MonkeyPatch) -> None:
    records = await _records(depth, monkeypatch)
    calls, by_seq = _calls(records), {r["seq"]: r for r in records}
    children = _js_children(records)
    for node, call in calls.items():
        mine = children[call["seq"]]
        assert mine, f"{node}: nothing nested under its call row"
        assert {by_seq[s]["payload"].get("delegate_node_id") for s in mine} == {node}, (node, mine)


async def test_the_two_readers_agree_on_which_run_each_call_holds(depth: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """They draw different rows of a run (the timeline folds its llm_call and tool_call records, the console its text and end), so compare the RUN each call holds."""
    records = await _records(depth, monkeypatch)
    py, js, by_seq = _py_children(records), _js_children(records), {r["seq"]: r for r in records}

    def runs(children: list[int]) -> set[str]:
        return {by_seq[s]["payload"]["delegate_run_id"] for s in children}

    for call in _calls(records).values():
        assert runs(py[call["seq"]]) == runs(js[call["seq"]]) and len(runs(js[call["seq"]])) == 1, (call["node_id"], py[call["seq"]], js[call["seq"]])


@pytest.mark.parametrize("workers", [3])
async def test_it_holds_for_more_instances(workers: int, depth: int, monkeypatch: pytest.MonkeyPatch) -> None:
    records = await _records(depth, monkeypatch, workers=workers, answers=workers)
    calls = _calls(records)
    assert sorted(calls) == [f"worker[{i}]" for i in range(workers)]
    for r in _delegated(records):
        assert r["seq"] > calls[r["payload"]["delegate_node_id"]]["seq"], r["seq"]


def test_the_journeys_seed_is_read_the_same_by_both_readers() -> None:
    """``tests/ui_e2e/_delegation_seed.build_fanout`` (what the CI-only journey opens) must say, to the timeline and to the console alike, that each run belongs under its own node's call."""
    from tests.ui_e2e import _delegation_seed as seed

    seeded = seed.build_fanout()
    by_seq = {r["seq"]: r for r in seeded.records}
    py, js = _py_children(seeded.records), _js_children(seeded.records)
    for call_seq, run in ((seeded.call_a_seq, seed.RUN_FAN_A), (seeded.call_b_seq, seed.RUN_FAN_B)):
        for name, children in (("timeline", py), ("console", js)):
            assert children[call_seq], f"{name}: nothing under call {call_seq}"
            assert {by_seq[s]["payload"]["delegate_run_id"] for s in children[call_seq]} == {run}, (name, call_seq, children[call_seq])
    parents_results = [r for r in seeded.records if r["kind"] == "tool_result" and not r["payload"].get("delegated")]
    assert {r["node_id"] for r in parents_results} == {"A", "B"}, "the parents' results carry their node, as production writes them"


# ---------------------------------------------------------------------------
# a node that waits for the drainer must never hang the turn
# ---------------------------------------------------------------------------


class _OneWorkerFails(_WorkerLLM):
    """Instance ``W1`` fails before it asks for anything; instance ``W0`` goes on to the tool (and so to the barrier)."""

    def stream(self, *, model, messages, **kw: Any):
        text = " ".join(part.text for m in messages if m.role == "user" for part in m.parts if hasattr(part, "text"))
        if "W1" in text:
            async def boom():
                await asyncio.sleep(0)
                raise RuntimeError("worker 1 fell over")
                yield  # pragma: no cover - makes this an async generator

            return boom()
        return super().stream(model=model, messages=messages, **kw)


async def _other_tasks() -> list[asyncio.Task]:
    me = asyncio.current_task()
    return [t for t in asyncio.all_tasks() if t is not me and not t.done()]


async def _tasks_that_outlive(seconds: float = 5) -> list[asyncio.Task]:
    """The tasks other than this one that are still running after they were given ``seconds`` to end (a cancelled task needs a loop turn or two to finish)."""
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(seconds):
            while await _other_tasks():
                await asyncio.sleep(0.01)
    return await _other_tasks()


class BarrierWatch:
    """Which graph nodes are AT the dispatch barrier right now (entered it, not released), and which waiters were cancelled while they waited."""

    def __init__(self) -> None:
        self.waiting: list[str | None] = []
        self.cancelled: list[str | None] = []


@pytest.fixture
def barrier_watch(monkeypatch: pytest.MonkeyPatch) -> BarrierWatch:
    """Instrument ``await_tool_dispatch_barrier`` as ``_stream_agent_node`` looks it up: the real one, wrapped to say who waits."""
    watch, real = BarrierWatch(), agent_node.await_tool_dispatch_barrier

    async def watched(queue: asyncio.Queue) -> None:
        node = current_graph_node_id()
        watch.waiting.append(node)
        try:
            await real(queue)
        except asyncio.CancelledError:
            watch.cancelled.append(node)
            raise
        finally:
            watch.waiting.remove(node)

    monkeypatch.setattr(agent_node, "await_tool_dispatch_barrier", watched)
    return watch


async def pull_until_a_node_waits(stream: Any, watch: BarrierWatch) -> None:
    """Pull events one at a time and, after each, let the node tasks run WITHOUT pulling again (so the drainer stays parked at its ``yield``) until a node is waiting at the barrier."""
    async for _event in stream:
        for _ in range(200):
            if watch.waiting:
                return
            await asyncio.sleep(0)
    pytest.fail("the stream ended before any node reached the barrier")


async def close_while_a_node_waits(ex: Any, watch: BarrierWatch, *, how: str) -> None:
    """Hold ``ex`` with a node AT the barrier, then close the stream (``how="aclose"``) or cancel a consumer that closes it in its ``finally``, as ``dispatch.py`` does on every exit path.

    Afterwards no task of the turn is left, and the waiter was CANCELLED at the barrier rather than left to a future nobody resolves."""
    ex.bind_coalesce_state(_CoalesceState())
    token = set_delegation_sink(DelegationRecorder(writer=_Log(), event_bus=_Bus(), session_id="s", turn_no=1))
    stream = ex.invoke([])
    try:
        async with asyncio.timeout(20):
            if how == "aclose":
                await pull_until_a_node_waits(stream, watch)
                assert watch.waiting, "the scene must have a node AT the barrier when the stream closes"
                await stream.aclose()
            else:
                holding = asyncio.Event()

                async def consumer() -> None:
                    try:
                        await pull_until_a_node_waits(stream, watch)
                        holding.set()
                        await asyncio.sleep(3600)   # the dispatch is busy elsewhere when the cancel lands
                    finally:
                        await stream.aclose()

                task = asyncio.create_task(consumer())
                await holding.wait()
                assert watch.waiting, "the scene must have a node AT the barrier when the consumer is cancelled"
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
    except TimeoutError:
        pytest.fail("closing the stream hung")
    finally:
        reset_delegation_sink(token)
    assert await _tasks_that_outlive() == [], "a node task outlived the closed stream"
    assert watch.cancelled, "the node that waited at the barrier was not cancelled there"


async def test_a_sibling_that_fails_while_a_node_awaits_the_barrier_still_ends_the_turn() -> None:
    """Instance ``W1`` fails at once; ``W0`` goes on to its tool and the barrier. The turn ends (no timeout, no task left), ``W0`` has its call row and its run in the log, and ``W1`` is exited as failed."""
    ex = await _executor(_OneWorkerFails(), answers=1)
    log = _Log()
    try:
        async with asyncio.timeout(20):
            records = await _run(ex, log=log)
    except TimeoutError:
        pytest.fail("the turn hung: a node waiting for the dispatch barrier never got it")
    assert await _tasks_that_outlive() == [], "a node task outlived the turn"
    calls = _calls(records)
    assert sorted(calls) == ["worker[0]"], "only the surviving instance made a call"
    assert [r["seq"] for r in _delegated(records) if r["seq"] < calls["worker[0]"]["seq"]] == [], "the run of W0 starts after its call row"
    assert any(r["kind"] == "tool_result" and r["node_id"] == "worker[0]" for r in records), "W0's call got its result"
    exits = {r["node_id"]: r["payload"]["status"] for r in records if r["kind"] == "graph_transition" and r["payload"].get("phase") == "exit"}
    assert exits["worker[1]"] == "failed" and exits["worker[0]"] == "completed", exits


@pytest.mark.parametrize("how", ["aclose", "cancel"])
async def test_closing_the_stream_while_a_node_waits_at_the_barrier_leaves_no_task_behind(
    barrier_watch: BarrierWatch, how: str, depth: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The consumer going away is the drainer's ``finally``: it cancels the node tasks, the one waiting at the barrier included (``aclose`` directly, or a consumer cancelled while it holds the stream).
    At depth 1 and 2 the waiter is a node of a CHILD graph: its barrier waits on the child's drainer, which waits on the parent's, so the close has to cancel through the chain."""
    await close_while_a_node_waits(await _executor(_WorkerLLM(), depth=depth, monkeypatch=monkeypatch), barrier_watch, how=how)
