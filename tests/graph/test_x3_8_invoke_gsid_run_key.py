"""X3.8: ``invoke_graph`` keys the child's graph-session subtree
(``graphs/<gsid>``) by the call's run identity (the node execution that makes
the call plus the call's position in it), never by the provider's tool call id
alone: Ollama and Gemini synthesise ``call_0``, ``call_1`` ... in every
response, so the same id repeats across calls and node executions, and a
repeated id used to hand one call's child session to another.
"""

import asyncio

import pytest

from primer.graph._node_identity import enter_node_run
from primer.graph.invoke_graph import GraphInvocationServices, run_invoke_graph
from primer.model.graph import Graph, _BeginNode, _EndNode, _StaticEdge


class _EndExec:
    """A child executor that ends immediately."""

    async def invoke(self, messages):
        from primer.graph.base import _GraphEndOutputEvent

        yield _GraphEndOutputEvent(text="ok", parsed=None, end_node_id="exit")


class _RecordingExec(_EndExec):
    """Records the gsid it was built for and the run id it was bound to."""

    def __init__(self, rec: list, gsid: str) -> None:
        self.rec = rec
        self._gsid = gsid

    def bind_run_id(self, run_id: str) -> None:
        self.rec.append((self._gsid, run_id))


def _svc(build, *, parent_gsid: str = "gs") -> GraphInvocationServices:
    async def _resolve(gid):
        return Graph(
            id=gid,
            description="stub child graph",
            nodes=[_BeginNode(id="begin"), _EndNode(id="exit")],
            edges=[_StaticEdge(from_node="begin", to_node="exit")],
        )

    return GraphInvocationServices(
        resolve_graph=_resolve,
        build_child_executor=build,
        session_id="s",
        workspace_id="w",
        graph_session_id=parent_gsid,
    )


@pytest.mark.asyncio
async def test_two_calls_in_one_node_execution_under_a_repeated_provider_id_get_two_run_keyed_subtrees() -> None:
    seen: list[tuple[str, str]] = []

    async def build(*, graph, gsid):
        return _RecordingExec(seen, gsid)

    svc = _svc(build)

    async def two_calls() -> None:
        enter_node_run("turn-1/agent@2")
        await run_invoke_graph(graph_id="g", graph_input="i", services=svc, tool_call_id="call_0")
        await run_invoke_graph(graph_id="g", graph_input="i", services=svc, tool_call_id="call_0")

    await asyncio.create_task(two_calls())
    # Keyed by position in the node execution, not by the (repeated) provider id.
    assert [g for g, _ in seen] == [
        "gs__invoke_turn-1_agent@2_invoke#1",
        "gs__invoke_turn-1_agent@2_invoke#2",
    ]
    # The bound run identity is the full run key (the path flattening is the
    # subtree's only concern).
    assert [r for _, r in seen] == [
        "turn-1/agent@2/invoke#1",
        "turn-1/agent@2/invoke#2",
    ]


@pytest.mark.asyncio
async def test_the_same_provider_call_id_in_two_executions_never_shares_a_subtree() -> None:
    gsids: list[str] = []

    async def build(*, graph, gsid):
        gsids.append(gsid)
        return _EndExec()

    svc = _svc(build)

    async def one_call_in(key: str) -> None:
        enter_node_run(key)
        await run_invoke_graph(graph_id="g", graph_input="i", services=svc, tool_call_id="call_0")

    await asyncio.create_task(one_call_in("turn-1/agent@1"))
    await asyncio.create_task(one_call_in("turn-2/agent@1"))
    assert gsids == [
        "gs__invoke_turn-1_agent@1_invoke#1",
        "gs__invoke_turn-2_agent@1_invoke#1",
    ]


@pytest.mark.asyncio
async def test_a_call_outside_a_graph_node_keeps_the_call_id_subtree_and_a_fresh_run() -> None:
    seen: list[tuple[str, str]] = []

    async def build(*, graph, gsid):
        return _RecordingExec(seen, gsid)

    svc = _svc(build)
    await run_invoke_graph(graph_id="g", graph_input="i", services=svc, tool_call_id="call_7")
    assert len(seen) == 1
    gsid, run_id = seen[0]
    assert gsid == "gs__invoke_call_7"
    assert run_id.startswith("invoke-")
