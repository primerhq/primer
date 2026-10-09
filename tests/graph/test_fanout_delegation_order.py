"""Concurrent graph siblings that delegate write each call row BEFORE the records of the run it started (ticket 01a11cca, the producer half).

``delegate_node_id`` says which node a delegated run belongs to, but both readers still need the CALL row to nest under, and the row has to be in the log first: ``_stream_agent_node``
only ``queue.put()``s the ``ToolCallEnd`` (the queue is unbounded), the loop dispatched the tool without waiting for the drainer, and ``run_subagent``'s recorder appends straight to
the writer. So with a REAL ``GraphExecutor`` fan-out of two workers that both call a delegating tool under raw id ``call_0`` the log read: both runs' first records, worker 0's call, the
rest of both runs, worker 1's call. The console put everything under worker 0's call and nothing under worker 1's; the timeline left the first records at the root.

A graph node whose executor has a coalesce state bound (session dispatch binds one every turn) now awaits the dispatch barrier before the in-process tool loop too, as the claims path always
did, so the drainer has written its calls before any tool runs. These cases drive the real executor, the real ``run_subagent``, the real ``DelegationRecorder`` and the real
``translate_stream_event`` the way ``dispatch.py`` wires them, and fold the log with both readers.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from primer.agent.invoke import build_subagent_toolmanager
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


async def _executor(llm: Any, *, workers: int = 2, answers: int = 2) -> GraphExecutor:
    storage, registry = _world([_text(f"sub answer {i}") for i in range(answers)])
    ctx = AgentResumeContext(session_id="s", workspace_id="ws-1", chat_id=None, principal="user-1", tools=["t1__delegate"], turn_no=1)

    async def tm_resolver(agent):
        return await build_subagent_toolmanager(ctx, storage_provider=storage, provider_registry=registry)

    async def agent_resolver(agent_id):
        return Agent(id=agent_id, description="x", model=AgentModel(profile_id="p--m"), system_prompt=[])

    async def llm_resolver(agent, *a, **k):
        return (llm, ResolvedModel(profile_id="p", provider_id="pv", model_name="m", context_length=128_000, config=ModelProfileConfig()))

    ts, ms = _InMemoryStorage(GraphThread), _InMemoryStorage(GraphNodeMessage)
    graph = _fanout_graph(workers)
    thread = await GraphExecutor.open_thread(graph=graph, thread_storage=ts, title="t")
    return GraphExecutor(
        graph=graph, agent_resolver=agent_resolver, llm_resolver=llm_resolver, thread_storage=ts, message_storage=ms,
        graph_thread_id=thread.id, router_registry=RouterRegistry(), tool_manager_resolver=tm_resolver,
    )


async def _run(ex: GraphExecutor, *, bind_state: bool = True) -> list[dict]:
    """The records session dispatch writes for this executor: its events through ``translate_stream_event`` with the coalesce state bound to the executor (``dispatch.py``)."""
    log, state = _Log(), _CoalesceState()
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


async def test_the_scene_is_two_workers_that_each_delegate_under_one_raw_id() -> None:
    """The control: the scene really is two instances, each with its own call row and its own run, all under ``call_0``."""
    records = await _run(await _executor(_WorkerLLM()))
    calls = _calls(records)
    assert sorted(calls) == ["worker[0]", "worker[1]"], sorted(calls)
    assert {c["payload"]["raw_id"] for c in calls.values()} == {"call_0"}
    runs = {r["payload"]["delegate_run_id"] for r in _delegated(records)}
    assert len(runs) == 2 and {r["payload"]["delegate_node_id"] for r in _delegated(records)} == {"worker[0]", "worker[1]"}


async def test_every_delegated_record_is_written_after_the_call_row_that_started_it() -> None:
    records = await _run(await _executor(_WorkerLLM()))
    calls = _calls(records)
    late = [
        (r["seq"], r["payload"]["delegate_node_id"], calls[r["payload"]["delegate_node_id"]]["seq"])
        for r in _delegated(records) if r["seq"] < calls[r["payload"]["delegate_node_id"]]["seq"]
    ]
    assert late == [], f"(seq of the delegated record, its node, seq of that node's call row): {late}"


async def test_the_timeline_puts_each_run_under_its_own_nodes_call() -> None:
    records = await _run(await _executor(_WorkerLLM()))
    calls, by_seq = _calls(records), {r["seq"]: r for r in records}
    children = _py_children(records)
    for node, call in calls.items():
        mine = children[call["seq"]]
        assert mine, f"{node}: nothing nested under its call row"
        assert {by_seq[s]["payload"].get("delegate_node_id") for s in mine} == {node}, (node, mine)
    folded = {r["seq"] for r in _delegated(records) if r["kind"] in _TIMELINE_KINDS}
    assert folded and {s for kids in children.values() for s in kids} >= folded, "every run's llm_call and tool_call records are under some call"


async def test_the_console_puts_each_run_under_its_own_nodes_call() -> None:
    records = await _run(await _executor(_WorkerLLM()))
    calls, by_seq = _calls(records), {r["seq"]: r for r in records}
    children = _js_children(records)
    for node, call in calls.items():
        mine = children[call["seq"]]
        assert mine, f"{node}: nothing nested under its call row"
        assert {by_seq[s]["payload"].get("delegate_node_id") for s in mine} == {node}, (node, mine)


async def test_the_two_readers_agree_on_which_run_each_call_holds() -> None:
    """They draw different rows of a run (the timeline folds its llm_call and tool_call records, the console its text and end), so compare the RUN each call holds."""
    records = await _run(await _executor(_WorkerLLM()))
    py, js, by_seq = _py_children(records), _js_children(records), {r["seq"]: r for r in records}

    def runs(children: list[int]) -> set[str]:
        return {by_seq[s]["payload"]["delegate_run_id"] for s in children}

    for call in _calls(records).values():
        assert runs(py[call["seq"]]) == runs(js[call["seq"]]) and len(runs(js[call["seq"]])) == 1, (call["node_id"], py[call["seq"]], js[call["seq"]])


@pytest.mark.parametrize("workers", [3])
async def test_it_holds_for_more_instances(workers: int) -> None:
    ex = await _executor(_WorkerLLM(), workers=workers, answers=workers)
    records = await _run(ex)
    calls = _calls(records)
    assert sorted(calls) == [f"worker[{i}]" for i in range(workers)]
    for r in _delegated(records):
        assert r["seq"] > calls[r["payload"]["delegate_node_id"]]["seq"], r["seq"]


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


async def test_a_sibling_that_fails_while_a_node_awaits_the_barrier_still_ends_the_turn() -> None:
    ex = await _executor(_OneWorkerFails(), answers=1)
    try:
        async with asyncio.timeout(20):
            records = await _run(ex)
    except TimeoutError:
        pytest.fail("the turn hung: a node waiting for the dispatch barrier never got it")
    except Exception:  # noqa: BLE001 - a failed node may surface as an error out of the stream; what matters is that the stream ended
        records = []
    assert await _other_tasks() == [], "a node task outlived the turn"
    assert isinstance(records, list)


async def test_closing_the_stream_while_a_node_awaits_the_barrier_leaves_no_task_behind() -> None:
    """The consumer going away is the drainer's ``finally``: it cancels the node tasks, the one awaiting the barrier's future included."""
    ex = await _executor(_WorkerLLM())
    ex.bind_coalesce_state(_CoalesceState())
    token = set_delegation_sink(DelegationRecorder(writer=_Log(), event_bus=_Bus(), session_id="s", turn_no=1))
    stream = ex.invoke([])
    try:
        async with asyncio.timeout(20):
            async for _event in stream:
                break   # the first event is enough: the nodes are mid-turn, some already at the barrier
            await stream.aclose()
    except TimeoutError:
        pytest.fail("closing the stream hung")
    finally:
        reset_delegation_sink(token)
    assert await _other_tasks() == [], "a node task outlived the closed stream"
