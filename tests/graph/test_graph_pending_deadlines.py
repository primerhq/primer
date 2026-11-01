"""Each pending entry of a graph park carries its own deadline, and the park waits until the earliest (GR-4, Task 103).

A node's wait (an approval with its policy timeout, ``ask_user(timeout_seconds=...)``, ``sleep(30)``) used to be dropped: the outer
``_approval`` yield carried no timeout, so every graph park and re-park waited 3600 s, and every re-park restarted the remaining waits' hour.

Characterization (passes before Task 103 and after it): a wait that names no timeout parks for the 3600 s cap; a checkpoint written before
entries carried a deadline re-parks for exactly 3600 s.

The review's probe ``g1_graph_timer_park.py`` (its park half) is inverted here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from primer.graph.executor import GraphExecutor
from primer.model.chat import ToolResultPart
from primer.model.graph import Graph, GraphNodeMessage, GraphThread, _BeginNode, _EndNode, _StaticEdge, _ToolCallNode
from primer.model.yield_ import ToolContext, Yielded, YieldToWorker
from primer.toolset.misc import _sleep_handler
from primer.worker.graph_resume_coordinator import repark_graph_outcome
from tests._resume_hook_fakes import make_toolcall_executor
from tests.graph.test_tool_wait_graph_park import _mk_parallel_executor, _patch_run_agent_turn
from tests.graph.test_toolcall_dispatch import _InMemoryStorage

SID = "s-dl"


def _graph(*nodes: str) -> Graph:
    return Graph(
        id="g-dl", description="parallel waits",
        nodes=[_BeginNode(id="begin"), *(_ToolCallNode(id=n, tool_id=f"x__{n}", arguments={}) for n in nodes),
               *(_EndNode(id=f"end{n}", output_template=n) for n in nodes)],
        edges=[*(_StaticEdge(from_node="begin", to_node=n) for n in nodes), *(_StaticEdge(from_node=n, to_node=f"end{n}") for n in nodes)],
    )


class _Waits:
    """Node ``n``'s first dispatch waits as ``waits[n]`` = (tool_name, timeout) says; the approved re-dispatch runs it."""

    def __init__(self, waits: dict[str, tuple[str, float | None]]) -> None:
        self.waits = waits

    async def __call__(self, node: Any, arguments: Any, bypass_approval: bool = False) -> ToolResultPart:
        if bypass_approval:
            return ToolResultPart(id=f"tc-{node.id}", output="ran")
        tool, timeout = self.waits[node.id]
        prefix = "tool_approval" if tool == "_approval" else "timer"
        raise YieldToWorker(Yielded(tool_name=tool, event_key=f"{prefix}:{SID}:{node.id}:tc-{node.id}", resume_metadata={}, timeout=timeout),
                            tool_call_id=f"tc-{node.id}")


class _Run:
    def __init__(self, waits: dict[str, tuple[str, float | None]]) -> None:
        self.graph = _graph(*waits)
        self.tools = _Waits(waits)
        self.ts: _InMemoryStorage[GraphThread] = _InMemoryStorage(GraphThread)
        self.ms: _InMemoryStorage[GraphNodeMessage] = _InMemoryStorage(GraphNodeMessage)
        self.thread: Any = None

    async def park(self) -> YieldToWorker:
        self.thread = await GraphExecutor.open_thread(graph=self.graph, thread_storage=self.ts, title="t")  # type: ignore[arg-type]
        with pytest.raises(YieldToWorker) as ei:
            async for _ev in make_toolcall_executor(self.graph, self.thread, self.ts, self.ms, self.tools).invoke([]):
                pass
        return ei.value

    async def resume(self, checkpoint: dict, node: str) -> YieldToWorker | None:
        ex = make_toolcall_executor(self.graph, self.thread, self.ts, self.ms, self.tools)
        try:
            async for _ev in ex.resume_from_checkpoint(checkpoint, toolcall_payload={"decision": "approved"}, resumed_tcid=f"tc-{node}",
                                                       resumed_event_key=f"tool_approval:{SID}:{node}:tc-{node}"):
                pass
        except YieldToWorker as yld:
            return yld
        return None


def _parked_for(yld: YieldToWorker) -> timedelta:
    park = repark_graph_outcome(None, SimpleNamespace(id=SID, turn_no=0), yld).park
    return park.parked_until - park.parked_at


def _deadlines(checkpoint: dict) -> dict[str, datetime]:
    entries = [*(checkpoint.get("pending_toolcalls") or []), *(checkpoint.get("pending_agent_yields") or [])]
    return {e["node_id"]: datetime.fromisoformat(e["deadline"]) for e in entries}


# ---- characterization ------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_wait_that_names_no_timeout_parks_for_the_cap() -> None:
    yld = await _Run({"a": ("_approval", None)}).park()

    assert timedelta(seconds=3599) < _parked_for(yld) <= timedelta(seconds=3600)


@pytest.mark.asyncio
async def test_a_checkpoint_without_entry_deadlines_reparks_for_exactly_the_cap() -> None:
    run = _Run({"a": ("_approval", None), "b": ("_approval", None)})
    checkpoint = (await run.park()).graph_checkpoint
    for entry in checkpoint["pending_toolcalls"]:
        entry.pop("deadline", None)  # as a park written before entries carried one

    repark = await run.resume(checkpoint, "a")

    assert repark is not None and [e["node_id"] for e in repark.graph_checkpoint["pending_toolcalls"]] == ["b"]
    assert _parked_for(repark) == timedelta(seconds=3600)


# ---- GR-4 ------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_graph_sleep_parks_until_its_own_timeout() -> None:
    """The probe's park half: the node's 30 s reaches the outer yield and the park."""
    inner = await _sleep_handler({"seconds": 30}, ctx=ToolContext(tool_call_id="tc-s", session_id=SID, workspace_id=None))
    assert inner.timeout == 30
    before = datetime.now(UTC)

    yld = await _Run({"a": ("sleep", inner.timeout)}).park()

    assert yld.yielded.timeout is not None and 29 < yld.yielded.timeout <= 30
    assert timedelta(seconds=29) < _parked_for(yld) <= timedelta(seconds=30)
    assert before + timedelta(seconds=30) <= _deadlines(yld.graph_checkpoint)["a"] <= datetime.now(UTC) + timedelta(seconds=30)


@pytest.mark.asyncio
async def test_a_park_on_several_waits_lasts_until_the_earliest() -> None:
    yld = await _Run({"a": ("_approval", 600.0), "b": ("sleep", 30.0)}).park()

    assert yld.yielded.event_key == f"tool_approval:{SID}:a:tc-a", "the approval stays the primary key"
    assert timedelta(seconds=29) < _parked_for(yld) <= timedelta(seconds=30)
    deadlines = _deadlines(yld.graph_checkpoint)
    assert timedelta(seconds=569) < deadlines["a"] - deadlines["b"] <= timedelta(seconds=571)


@pytest.mark.asyncio
async def test_a_repark_keeps_each_remaining_waits_deadline() -> None:
    run = _Run({"a": ("_approval", 600.0), "b": ("_approval", 900.0)})
    first = await run.park()
    b_deadline = _deadlines(first.graph_checkpoint)["b"]

    repark = await run.resume(first.graph_checkpoint, "a")

    assert repark is not None
    assert _deadlines(repark.graph_checkpoint) == {"b": b_deadline}
    assert _parked_for(repark) <= b_deadline - datetime.now(UTC) + timedelta(seconds=1)


@pytest.mark.asyncio
async def test_an_agent_node_wait_carries_its_own_deadline(monkeypatch) -> None:
    ex = await _mk_parallel_executor()
    asks = {
        node: YieldToWorker(Yielded(tool_name="ask_user", event_key=f"ask_user:{SID}:{node}:tc-{node}", resume_metadata={"prompt": "?"},
                                    timeout=timeout), tool_call_id=f"tc-{node}", llm_messages=[])
        for node, timeout in (("A", 86400.0), ("B", 45.0))
    }
    _patch_run_agent_turn(monkeypatch, {"agent-a": asks["A"], "agent-b": asks["B"]})

    with pytest.raises(YieldToWorker) as ei:
        async for _ev in ex.invoke([]):
            pass

    deadlines = _deadlines(ei.value.graph_checkpoint)
    assert timedelta(seconds=86354) < deadlines["A"] - deadlines["B"] <= timedelta(seconds=86356)
    assert timedelta(seconds=44) < _parked_for(ei.value) <= timedelta(seconds=45)
