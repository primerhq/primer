"""Unit tests for invoke_graph happy path (non-parking).

invoke_graph runs a target graph inside the current workspace session,
namespaced under the session's state, and returns its output text. These
tests exercise ``run_invoke_graph`` directly against stub executors,
covering both the canonical end-output event (the real path a graph
executor takes) and the text-delta accumulation path mirrored from
``_stream_subgraph_node``.
"""

import pytest

from primer.graph.invoke_graph import GraphInvocationServices, run_invoke_graph


class _TextDeltaLike:
    """A minimal stand-in for a raw text-delta stream event.

    ``_stream_subgraph_node`` collects subgraph text via
    ``getattr(sub_event, "type", None) == "text-delta"`` then
    ``getattr(sub_event, "text", None)`` — duck-typed, never an
    isinstance check — so a plain object with those two attributes is a
    faithful stand-in for that code path.
    """

    def __init__(self, text: str):
        self.type = "text-delta"
        self.text = text


def _end_event(text: str):
    from primer.graph.base import _GraphEndOutputEvent

    return _GraphEndOutputEvent(text=text, parsed=None, end_node_id="end")


class _EndOutputExec:
    """Yields a single terminal _GraphEndOutputEvent (the real path)."""

    async def invoke(self, messages):
        yield _end_event("graph said hi")


class _TextDeltaExec:
    """Yields raw text-delta-typed events (subgraph mirror path)."""

    async def invoke(self, messages):
        yield _TextDeltaLike("graph ")
        yield _TextDeltaLike("said hi")


async def _resolve_graph(gid):
    # The resolved object is opaque to run_invoke_graph; it only passes it
    # through to build_child_executor (a stub here). A sentinel suffices.
    return {"id": gid}


@pytest.mark.asyncio
async def test_invoke_graph_returns_end_output_and_namespaces_gsid():
    captured = {}

    async def _build(*, graph, gsid):
        captured["gsid"] = gsid
        return _EndOutputExec()

    svc = GraphInvocationServices(
        resolve_graph=_resolve_graph,
        build_child_executor=_build,
        session_id="sess-1",
        workspace_id="ws-1",
        graph_session_id="gs-1",
    )
    out = await run_invoke_graph(
        graph_id="graph-x",
        graph_input="do it",
        services=svc,
        tool_call_id="tc1",
    )
    assert out == "graph said hi"
    assert captured["gsid"] == "gs-1__invoke_tc1"


@pytest.mark.asyncio
async def test_invoke_graph_accumulates_text_deltas():
    async def _build(*, graph, gsid):
        return _TextDeltaExec()

    svc = GraphInvocationServices(
        resolve_graph=_resolve_graph,
        build_child_executor=_build,
        session_id="sess-1",
        workspace_id="ws-1",
        graph_session_id="gs-1",
    )
    out = await run_invoke_graph(
        graph_id="graph-x",
        graph_input="do it",
        services=svc,
        tool_call_id="tc2",
    )
    assert out == "graph said hi"
