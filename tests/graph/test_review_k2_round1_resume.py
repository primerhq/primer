"""Lane K2 fix round 1: a graph run's persisted state is keyed to ONE run, and a resume continues it exactly.

The reclaim resume (review 2026-11-23, 6.2) reads ``state.json`` from the run's state subtree. The review of
that fix found the subtree is shared by runs that are not the same run (a reopened or restarted session, a
subgraph node that runs twice, two ``invoke_graph`` calls under one synthesized provider call id), and that
the persisted state lacked what the next superstep needs (the fan-out bookkeeping, the ready set, the
iteration a resumed batch runs at). These tests drive the real ``WorkspaceGraphExecutor`` against a
file-backed state repo and resume from HARD-CRASH snapshots: a copy of the whole ``.state`` tree taken at a
commit, as a worker that died right after that commit leaves it.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from primer.graph.base import _GraphEndOutputEvent, _GraphErrorEvent
from primer.graph.invoke_graph import GraphInvocationServices, run_invoke_graph
from primer.graph.router import RouterRegistry
from primer.graph.workspace_executor import WorkspaceGraphExecutor
from primer.model.chat import Done, Message, StreamEvent, TextDelta, TextPart
from primer.model.graph import (
    FanOutSpec,
    Graph,
    _AgentNodeRef,
    _BeginNode,
    _CallableRouter,
    _ConditionalEdge,
    _EndNode,
    _FanInNode,
    _FanOutNode,
    _GraphNodeRef,
    _StaticEdge,
)
from primer.model.workspace_session import SessionStatus
from primer.model.yield_ import Yielded, YieldToWorker
from tests.graph.test_review_6_2_reclaim_resume_shapes import (
    _agent,
    _drain,
    _enter_events,
    _graph,
    _model,
    _RecordingLLM,
    _StubStateRepo,
)
from tests.graph.test_workspace_executor import _RecordingHolder


class _SnapRepo(_StubStateRepo):
    """Copies the whole ``.state`` tree after every commit: each copy is a hard-crash point."""

    def __init__(self, root: Path, snaps: Path) -> None:
        super().__init__(root, workspace_id="ws")
        self.snaps = snaps
        self.states: list[dict[str, dict]] = []

    async def commit_arbitrary(self, **kw: Any) -> str:
        sha = await super().commit_arbitrary(**kw)
        shutil.copytree(self._path, self.snaps / str(len(self.states) + 1))
        self.states.append({
            k: json.loads(v) for k, v in (kw.get("files") or {}).items() if k.endswith("state.json")
        })
        return sha


def _snap(repo: _SnapRepo, gsid: str, pred) -> Path:
    """The snapshot of the FIRST commit whose ``gsid`` state.json matches ``pred``."""
    for i, files in enumerate(repo.states, start=1):
        st = files.get(f"graphs/{gsid}/state.json")
        if st is not None and pred(st):
            return repo.snaps / str(i)
    raise AssertionError("no commit matches the crash point")


async def _crashed_repo(snap: Path, root: Path) -> _StubStateRepo:
    repo = _StubStateRepo(root, "ws")
    shutil.copytree(snap, root)
    return repo


def _status(st: dict, nid: str) -> str | None:
    return (st["node_states"].get(nid) or {}).get("status")


class _CountingLLM:
    """Answers ``answer-<n>`` on its n-th call, so a re-run is told apart from a replayed output."""

    def __init__(self) -> None:
        self.calls: list[list[Message]] = []

    async def list_models(self):
        return ["m"]

    def stream(self, *, model: str, messages: list[Message], **kwargs: Any) -> AsyncIterator[StreamEvent]:
        self.calls.append(list(messages))
        n = len(self.calls)

        async def _g():
            yield TextDelta(text=f"answer-{n}", index=0)
            yield Done(stop_reason="stop", raw_reason="stop")

        return _g()


class _ParkingLLM:
    """An agent node that asks the user: the node parks the run."""

    def __init__(self) -> None:
        self.calls = 0

    async def list_models(self):
        return ["m"]

    def stream(self, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        self.calls += 1

        async def _g():
            raise YieldToWorker(
                Yielded(tool_name="ask_user", event_key="ask_user:s:tc1", resume_metadata={"prompt": "colour?"}),
                tool_call_id="tc1",
                llm_messages=[Message(role="assistant", parts=[TextPart(text="(asking)")]).model_dump(mode="json")],
            )
            yield  # pragma: no cover

        return _g()


async def _build(
    repo: _StubStateRepo,
    llms: dict[str, Any],
    gsid: str,
    graph: Graph,
    *,
    run_id: str | None = None,
    turn_no: int | None = None,
    owns: bool = True,
    holder: Any = None,
    graph_resolver: Any = None,
    router_registry: RouterRegistry | None = None,
) -> WorkspaceGraphExecutor:
    async def agent_resolver(agent_id: str):
        return _agent(agent_id)

    async def llm_resolver(agent):
        return (llms[agent.id], _model())

    ex = WorkspaceGraphExecutor(
        graph=graph,
        agent_resolver=agent_resolver,
        llm_resolver=llm_resolver,  # type: ignore[arg-type]
        state_repo=repo,
        graph_session_id=gsid,
        owns_session_lifecycle=owns,
        workspace_session=holder,
        graph_resolver=graph_resolver,
        router_registry=router_registry,
        turn_no=turn_no,
    )
    if run_id is not None:
        ex.bind_run_id(run_id)
    return ex


def _linear(n: int, *, end_template: str | None = None) -> Graph:
    ids = [f"n{i}" for i in range(1, n + 1)]
    chain = ["begin", *ids, "exit"]
    return _graph(
        nodes=[
            _BeginNode(id="begin"),
            *[_AgentNodeRef(id=nid, agent_id=f"a{nid[1:]}") for nid in ids],
            _EndNode(id="exit", **({"output_template": end_template} if end_template else {})),
        ],
        edges=[_StaticEdge(from_node=a, to_node=b) for a, b in zip(chain, chain[1:])],
    )


def _end_texts(events: list) -> list[str]:
    return [ev.text for ev in events if isinstance(ev, _GraphEndOutputEvent)]


# ---- Critical 1: a run identity, not the state subtree, decides what is resumed --------------------------------------


@pytest.mark.asyncio
async def test_a_new_turn_on_the_same_session_runs_fresh(tmp_path: Path) -> None:
    repo = _StubStateRepo(tmp_path / ".state", "ws")
    await repo.initialize()
    g = _linear(2, end_template="{{ nodes.n2.text }}")
    llms = {"a1": _CountingLLM(), "a2": _CountingLLM()}
    await _drain((await _build(repo, llms, "gs", g, turn_no=3)).invoke([]))

    # The session's next turn (its row's turn_no moved on) reuses the same state subtree.
    second = await _build(repo, llms, "gs", g, turn_no=4)
    events = await _drain(second.invoke([]))

    assert "begin" in _enter_events(events), "the next turn replayed the previous turn's outcome instead of running"
    assert (len(llms["a1"].calls), len(llms["a2"].calls)) == (2, 2)
    assert _end_texts(events) == ["answer-2"]
    assert second._last_ended_reason == "completed"


@pytest.mark.asyncio
async def test_a_reopened_completed_run_starts_fresh(tmp_path: Path) -> None:
    repo = _StubStateRepo(tmp_path / ".state", "ws")
    await repo.initialize()
    g = _linear(2, end_template="{{ nodes.n2.text }}")
    llms = {"a1": _CountingLLM(), "a2": _CountingLLM()}
    await _drain((await _build(repo, llms, "gs", g, run_id="1.5")).invoke([]))

    # Reopen (or restart) keeps turn_no; the invocation moves on, and so does the run identity.
    reopened = await _build(repo, llms, "gs", g, run_id="2.5")
    events = await _drain(reopened.invoke([]))

    assert "begin" in _enter_events(events)
    assert (len(llms["a1"].calls), len(llms["a2"].calls)) == (2, 2)
    assert _end_texts(events) == ["answer-2"]
    assert reopened._last_ended_reason == "completed"


@pytest.mark.asyncio
async def test_a_cancelled_run_restarted_starts_fresh_while_its_reclaim_resumes(tmp_path: Path) -> None:
    g = _linear(2)
    llms = {"a1": _RecordingLLM(), "a2": _RecordingLLM()}
    repo = _StubStateRepo(tmp_path / ".state", "ws")
    await repo.initialize()
    first = await _build(repo, llms, "gs", g, run_id="1.0")
    gen = first.invoke([])
    async for ev in gen:
        if getattr(getattr(ev, "extended", None), "node_id", None) == "n2":
            break
    await gen.aclose()  # a cancel: the run is saved ENDED/cancelled mid-n2
    assert (await first.load_state())["ended_reason"] == "cancelled"

    # The SAME turn reclaimed resumes the cut superstep: n2 only.
    snapshot = tmp_path / "snap"
    shutil.copytree(repo.path, snapshot)
    reclaimed = await _build(repo, llms, "gs", g, run_id="1.0")
    events = await _drain(reclaimed.invoke([]))
    assert _enter_events(events) == ["n2", "exit"]

    # A restart of the cancelled session is a new run: everything runs again.
    restarted_repo = await _crashed_repo(snapshot, tmp_path / "r2" / ".state")
    llms2 = {"a1": _RecordingLLM(), "a2": _RecordingLLM()}
    restarted = await _build(restarted_repo, llms2, "gs", g, run_id="2.0")
    events = await _drain(restarted.invoke([]))
    assert _enter_events(events) == ["begin", "n1", "n2", "exit"]
    assert restarted._last_ended_reason == "completed"


@pytest.mark.asyncio
async def test_a_run_cancelled_while_parked_then_reopened_starts_fresh(tmp_path: Path) -> None:
    g = _linear(1, end_template="{{ nodes.n1.text }}")
    repo = _StubStateRepo(tmp_path / ".state", "ws")
    await repo.initialize()
    parking = {"a1": _ParkingLLM()}
    with pytest.raises(YieldToWorker):
        await _drain((await _build(repo, parking, "gs", g, run_id="1.0")).invoke([]))
    assert (await (await _build(repo, parking, "gs", g)).load_state())["status"] == SessionStatus.WAITING.value

    # The operator cancels the parked session (no turn runs) and a person reopens it.
    llms = {"a1": _CountingLLM()}
    reopened = await _build(repo, llms, "gs", g, run_id="2.0")
    events = await _drain(reopened.invoke([]))

    assert _enter_events(events) == ["begin", "n1", "exit"]
    assert _end_texts(events) == ["answer-1"]
    assert reopened._last_ended_reason == "completed"


@pytest.mark.asyncio
async def test_a_reclaimed_park_whose_record_was_lost_reparks_not_a_false_success(tmp_path: Path) -> None:
    g = _linear(1)
    repo = _StubStateRepo(tmp_path / ".state", "ws")
    await repo.initialize()
    parking = _ParkingLLM()
    with pytest.raises(YieldToWorker):
        await _drain((await _build(repo, {"a1": parking}, "gs", g, run_id="1.0")).invoke([]))

    # The worker died after the WAITING commit and before the park reached the row: the same turn runs again.
    again = await _build(repo, {"a1": parking}, "gs", g, run_id="1.0")
    events: list = []
    with pytest.raises(YieldToWorker):
        async for ev in again.invoke([]):
            events.append(ev)
    assert "begin" not in _enter_events(events), "the parked superstep re-runs; Begin does not"
    assert parking.calls == 2, "the parked node asks again"
    assert again.last_done_reason is None, "a re-parked run reports no outcome"


def _cyclic_subgraph_parent() -> tuple[Graph, Graph]:
    child = _graph(
        nodes=[_BeginNode(id="begin"), _AgentNodeRef(id="c1", agent_id="ac"), _EndNode(id="exit", output_template="{{ nodes.c1.text }}")],
        edges=[_StaticEdge(from_node="begin", to_node="c1"), _StaticEdge(from_node="c1", to_node="exit")],
    )
    parent = Graph(
        id="g-parent", description="loop over a subgraph", max_iterations=3, on_max_iterations="exit",
        nodes=[_BeginNode(id="begin"), _GraphNodeRef(id="sub", graph_id="child"), _EndNode(id="exit", output_template="{{ nodes.sub.text }}")],
        edges=[_StaticEdge(from_node="begin", to_node="sub"), _StaticEdge(from_node="sub", to_node="sub")],
    )
    return parent, child


@pytest.mark.asyncio
async def test_a_subgraph_node_that_runs_twice_runs_its_child_twice(tmp_path: Path) -> None:
    parent, child = _cyclic_subgraph_parent()
    llms = {"ac": _CountingLLM()}
    repo = _StubStateRepo(tmp_path / ".state", "ws")
    await repo.initialize()

    async def resolve(gid: str) -> Graph:
        return child

    ex = await _build(repo, llms, "gs", parent, graph_resolver=resolve)
    events = await _drain(ex.invoke([]))

    subs = _enter_events(events).count("sub")
    assert subs >= 2
    assert len(llms["ac"].calls) == subs, f"the subgraph node ran {subs} times, its child {len(llms['ac'].calls)}"
    # The last execution's own End output is the run's result, not a replay of the first one.
    assert _end_texts(events)[-1] == f"answer-{subs}"


def _invoke_services(repo: _StubStateRepo, llms: dict[str, Any]) -> GraphInvocationServices:
    child = _graph(
        nodes=[_BeginNode(id="begin"), _AgentNodeRef(id="c1", agent_id="ac"), _EndNode(id="exit", output_template="{{ nodes.c1.text }}")],
        edges=[_StaticEdge(from_node="begin", to_node="c1"), _StaticEdge(from_node="c1", to_node="exit")],
    )

    async def resolve(gid: str) -> Graph:
        return child

    async def build(*, graph, gsid):
        return await _build(repo, llms, gsid, graph, owns=False)

    return GraphInvocationServices(
        resolve_graph=resolve, build_child_executor=build, session_id="s", workspace_id="ws", graph_session_id="s",
    )


@pytest.mark.asyncio
async def test_invoke_graph_under_a_repeated_provider_call_id_runs_each_child(tmp_path: Path) -> None:
    repo = _StubStateRepo(tmp_path / ".state", "ws")
    await repo.initialize()
    llms = {"ac": _CountingLLM()}
    svc = _invoke_services(repo, llms)

    # Ollama and Gemini number their calls call_0, call_1 ... in every response.
    first = await run_invoke_graph(graph_id="child", graph_input="one", services=svc, tool_call_id="call_0")
    second = await run_invoke_graph(graph_id="child", graph_input="two", services=svc, tool_call_id="call_0")

    assert (first, second) == ("answer-1", "answer-2"), "the second invocation returned the first child's output"
    assert len(llms["ac"].calls) == 2


@pytest.mark.asyncio
async def test_invoke_graph_in_a_node_execution_keys_its_children_by_call_position(tmp_path: Path) -> None:
    repo = _StubStateRepo(tmp_path / ".state", "ws")
    await repo.initialize()
    llms = {"ac": _CountingLLM()}
    svc = _invoke_services(repo, llms)

    from primer.graph._node_identity import enter_node_run

    async def node_execution(calls: int) -> list[str]:
        # What the superstep loop publishes for every node task: the run, the node instance, the iteration.
        enter_node_run("1.0/n1@1")
        return [
            await run_invoke_graph(graph_id="child", graph_input="x", services=svc, tool_call_id="call_0")
            for _ in range(calls)
        ]

    # Two calls in one node execution under one synthesized id are two children.
    assert await asyncio.create_task(node_execution(2)) == ["answer-1", "answer-2"]

    # A node execution re-run after a reclaim makes its calls again in the same positions: its first call
    # is the persisted first child, which is resumed (it replays its End output, no model call).
    repo2 = _StubStateRepo(tmp_path / "r2" / ".state", "ws")
    await repo2.initialize()
    llms2 = {"ac": _CountingLLM()}
    svc2 = _invoke_services(repo2, llms2)

    async def one_call() -> str:
        enter_node_run("1.0/n1@1")
        return await run_invoke_graph(graph_id="child", graph_input="x", services=svc2, tool_call_id="call_0")

    assert await asyncio.create_task(one_call()) == "answer-1"
    assert await asyncio.create_task(one_call()) == "answer-1", "a reclaimed node's child is resumed, not re-run"
    assert len(llms2["ac"].calls) == 1


@pytest.mark.asyncio
async def test_the_superstep_loop_publishes_each_node_execution(tmp_path: Path) -> None:
    from primer.graph._node_identity import current_node_run

    seen: list[str | None] = []

    class _Peeking(_RecordingLLM):
        def stream(self, **kwargs: Any):
            run = current_node_run()
            seen.append(run.key if run is not None else None)
            return super().stream(**kwargs)

    repo = _StubStateRepo(tmp_path / ".state", "ws")
    await repo.initialize()
    g = _linear(2)
    await _drain((await _build(repo, {"a1": _Peeking(), "a2": _Peeking()}, "gs", g, run_id="7.2")).invoke([]))
    assert seen == ["7.2/n1@1", "7.2/n2@2"]


# ---- Critical 2: fan-out resume ----------------------------------------------------------------------------------------


def _fanout_graph() -> Graph:
    return Graph(
        id="g-fo", description="fan-out", max_iterations=None,
        nodes=[
            _BeginNode(id="begin"),
            _FanOutNode(id="fo", specs=[FanOutSpec(kind="broadcast", target_node_id="w", count=2)]),
            _AgentNodeRef(id="w", agent_id="aw"),
            _FanInNode(id="fi"),
            _EndNode(id="exit", output_template="{{ nodes.w | length }}"),
        ],
        edges=[_StaticEdge(from_node="begin", to_node="fo"), _StaticEdge(from_node="w", to_node="fi"),
               _StaticEdge(from_node="fi", to_node="exit")],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("point", "reruns"), [
    ("fanout_settled", 2),      # FanOut done, its copies not started
    ("instances_running", 2),   # the copies in flight
    ("instances_settled", 0),   # the copies done, FanIn not started
])
async def test_a_fanout_cut_resumes_to_the_end(tmp_path: Path, point: str, reruns: int) -> None:
    g = _fanout_graph()
    r0 = _SnapRepo(tmp_path / "r0" / ".state", tmp_path / "s0")
    await r0.initialize()
    await _drain((await _build(r0, {"aw": _RecordingLLM()}, "gs", g)).invoke([]))
    preds = {
        "fanout_settled": lambda st: _status(st, "fo") == "ended" and _status(st, "w[0]") is None,
        "instances_running": lambda st: _status(st, "w[0]") == "running",
        "instances_settled": lambda st: st["status"] == "running" and _status(st, "w[0]") == "ended" and _status(st, "fi") == "pending",
    }
    repo = await _crashed_repo(_snap(r0, "gs", preds[point]), tmp_path / "r1" / ".state")
    llm = _RecordingLLM()
    ex = await _build(repo, {"aw": llm}, "gs", g)
    events = await _drain(ex.invoke([]))

    assert len(llm.calls) == reruns
    assert "fi" in _enter_events(events) and "exit" in _enter_events(events)
    assert _end_texts(events) == ["2"], "a resumed fan-out must reach its End"
    assert ex._last_ended_reason == "completed"
    assert not [e for e in events if isinstance(e, _GraphErrorEvent)]


# ---- Important 3: two successive hard crashes ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_successive_hard_crashes_never_rerun_a_settled_node(tmp_path: Path) -> None:
    g = _linear(3)
    r0 = _SnapRepo(tmp_path / "r0" / ".state", tmp_path / "s0")
    await r0.initialize()
    await _drain((await _build(r0, {k: _RecordingLLM() for k in ("a1", "a2", "a3")}, "gs", g)).invoke([]))
    after_n1 = lambda st: st["status"] == "running" and _status(st, "n1") == "ended" and _status(st, "n2") == "pending"  # noqa: E731
    after_n2 = lambda st: st["status"] == "running" and _status(st, "n2") == "ended" and _status(st, "n3") == "pending"  # noqa: E731

    # Crash 1, between the supersteps of n1 and n2.
    r1 = _SnapRepo(tmp_path / "r1" / ".state", tmp_path / "s1")
    shutil.copytree(_snap(r0, "gs", after_n1), r1.path)
    llms1 = {k: _RecordingLLM() for k in ("a1", "a2", "a3")}
    await _drain((await _build(r1, llms1, "gs", g)).invoke([]))
    assert [len(llms1[k].calls) for k in ("a1", "a2", "a3")] == [0, 1, 1]

    # Crash 2, inside the first reclaim, between n2 and n3.
    r2 = await _crashed_repo(_snap(r1, "gs", after_n2), tmp_path / "r2" / ".state")
    llms2 = {k: _RecordingLLM() for k in ("a1", "a2", "a3")}
    ex = await _build(r2, llms2, "gs", g)
    events = await _drain(ex.invoke([]))

    assert [len(llms2[k].calls) for k in ("a1", "a2", "a3")] == [0, 0, 1], "a node settled before the second crash re-ran"
    assert _enter_events(events) == ["n3", "exit"]
    assert all(m.role != "assistant" for m in llms2["a3"].calls[0]["messages"])
    assert ex._last_ended_reason == "completed"


# ---- Minors -------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_resume_never_routes_again_so_a_raising_router_cannot_escape(tmp_path: Path) -> None:
    calls: list[int] = []

    def pick(context, source) -> str:
        calls.append(1)
        if len(calls) > 1:
            raise RuntimeError("router broke")
        return "exit"

    registry = RouterRegistry()
    registry.register("pick", pick)
    g = _graph(
        nodes=[_BeginNode(id="begin"), _AgentNodeRef(id="n1", agent_id="a1"), _EndNode(id="exit")],
        edges=[_StaticEdge(from_node="begin", to_node="n1"),
               _ConditionalEdge(from_node="n1", router=_CallableRouter(callable_id="pick"))],
    )
    r0 = _SnapRepo(tmp_path / "r0" / ".state", tmp_path / "s0")
    await r0.initialize()
    await _drain((await _build(r0, {"a1": _RecordingLLM()}, "gs", g, router_registry=registry)).invoke([]))
    repo = await _crashed_repo(
        _snap(r0, "gs", lambda st: st["status"] == "running" and _status(st, "n1") == "ended"), tmp_path / "r1" / ".state",
    )

    ex = await _build(repo, {"a1": _RecordingLLM()}, "gs", g, router_registry=registry)
    events = await _drain(ex.invoke([]))  # must not raise

    assert len(calls) == 1, "the resume routed the settled superstep again"
    assert _enter_events(events) == ["exit"]
    assert ex._last_ended_reason == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("owns", [False, True])
async def test_a_crash_after_end_before_the_final_save_finishes_like_a_normal_end(tmp_path: Path, owns: bool) -> None:
    g = _linear(1, end_template="{{ nodes.n1.text }}")
    r0 = _SnapRepo(tmp_path / "r0" / ".state", tmp_path / "s0")
    await r0.initialize()
    await _drain((await _build(r0, {"a1": _CountingLLM()}, "gs", g, owns=owns)).invoke([]))
    assert r0.states[-1]["graphs/gs/state.json"]["status"] == "ended"
    repo = await _crashed_repo(r0.snaps / str(len(r0.states) - 1), tmp_path / "r1" / ".state")

    holder = _RecordingHolder()
    llm = _CountingLLM()
    ex = await _build(repo, {"a1": llm}, "gs", g, owns=owns, holder=holder)
    events = await _drain(ex.invoke([]))

    assert len(llm.calls) == 0, "n1 settled before the crash; it must not run again"
    assert _end_texts(events) == ["answer-1"], "the End output is emitted"
    st = await ex.load_state()
    assert (st["status"], st["ended_reason"]) == ("ended", "completed")
    assert ex._last_ended_reason == "completed"
    assert holder.status_calls == ([(SessionStatus.ENDED, "completed")] if owns else [])
