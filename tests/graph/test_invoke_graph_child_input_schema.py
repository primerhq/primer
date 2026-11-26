"""Review 2026-11-23 finding 7.1b: invoke_graph never checks the child graph's input_schema.

``run_invoke_graph`` (``primer/graph/invoke_graph.py``) resolves the child graph and runs it without the
pre-check the session-create path gives a graph's ``Begin.input_schema`` (``primer/workspace/
session_factory.py``): the schema is author-supplied and the instance is model-supplied, so its regex
keywords must run bounded and off the event loop (``validate_bounded_off_loop``, label ``input_schema``),
and an invalid input is refused with the same error the session-create path gives.
"""

import time

import pytest

from primer.graph.invoke_graph import GraphInvocationServices, run_invoke_graph
from primer.model.except_ import ValidationError
from primer.model.graph import Graph, _BeginNode, _EndNode, _StaticEdge


def _child_graph(input_schema: dict | None) -> Graph:
    return Graph(
        id="g-child",
        description="child graph with an input contract",
        nodes=[_BeginNode(id="begin", input_schema=input_schema), _EndNode(id="exit")],
        edges=[_StaticEdge(from_node="begin", to_node="exit")],
    )


class _RecordingChild:
    """A child executor stub that records what it was invoked with."""

    def __init__(self) -> None:
        self.invoked_with = None

    async def invoke(self, messages):
        self.invoked_with = messages
        yield  # no events: the output channel under test is not the stream


def _services(child, schema: dict | None) -> GraphInvocationServices:
    async def _resolve_graph(graph_id: str) -> Graph:
        return _child_graph(schema)

    async def _build(*, graph, gsid):
        return child

    return GraphInvocationServices(
        resolve_graph=_resolve_graph,
        build_child_executor=_build,
        session_id="sess-1",
        workspace_id="ws-1",
        graph_session_id="gs-1",
    )


@pytest.mark.asyncio
async def test_a_valid_child_input_passes_through_to_the_child():
    child = _RecordingChild()
    out = await run_invoke_graph(
        graph_id="g-child",
        graph_input="Review the diff.",
        services=_services(child, {"type": "string", "minLength": 1}),
        tool_call_id="tc1",
    )
    assert out == ""  # the stub child yields no end-output / text-delta events
    assert child.invoked_with is not None, "the child must run when its input validates"
    texts = [part.text for msg in child.invoked_with for part in msg.parts]
    assert "Review the diff." in texts


@pytest.mark.asyncio
async def test_a_json_object_input_passes_an_object_input_schema():
    # The tool's ``input`` is a string: a child whose Begin expects an object is invoked with
    # the object JSON-encoded, and it is that object the schema checks.
    child = _RecordingChild()
    await run_invoke_graph(
        graph_id="g-child",
        graph_input='{"task": "triage"}',
        services=_services(child, {"type": "object", "required": ["task"], "properties": {"task": {"type": "string"}}}),
        tool_call_id="tc4",
    )
    assert child.invoked_with is not None, "a JSON-encoded object that matches the schema must start the child"


@pytest.mark.asyncio
async def test_a_json_object_input_that_breaks_the_schema_is_refused():
    child = _RecordingChild()
    with pytest.raises(ValidationError) as exc_info:
        await run_invoke_graph(
            graph_id="g-child",
            graph_input='{"task": 7}',
            services=_services(child, {"type": "object", "properties": {"task": {"type": "string"}}}),
            tool_call_id="tc5",
        )
    assert "graph_input invalid at path ['task']" in str(exc_info.value)
    assert child.invoked_with is None


@pytest.mark.asyncio
async def test_an_invalid_child_input_is_refused_with_the_session_create_error():
    child = _RecordingChild()
    with pytest.raises(ValidationError) as exc_info:
        await run_invoke_graph(
            graph_id="g-child",
            graph_input="a plain string",
            services=_services(child, {"type": "object", "properties": {"task": {"type": "string"}}}),
            tool_call_id="tc2",
        )
    # The same refusal the session-create path gives for the same schema + value.
    assert "graph_input invalid at path" in str(exc_info.value)
    assert "is not of type 'object'" in str(exc_info.value)
    assert child.invoked_with is None, "a refused input must not start the child run"


@pytest.mark.asyncio
async def test_a_backtracking_child_pattern_is_refused_bounded_instead_of_stalling():
    child = _RecordingChild()
    # The measured hazard (review 2026-11-23, 7.1): (a|aa)+ on a long non-matching
    # value grows about 1.6x per character; unbounded this takes minutes.
    started = time.perf_counter()
    with pytest.raises(ValidationError) as exc_info:
        await run_invoke_graph(
            graph_id="g-child",
            graph_input="a" * 60 + "b",
            services=_services(child, {"type": "string", "pattern": r"(a|aa)+$"}),
            tool_call_id="tc3",
        )
    elapsed = time.perf_counter() - started
    assert "cannot be validated in time" in str(exc_info.value)
    assert elapsed < 5.0, f"the bounded validation took {elapsed:.1f} s; the pattern ran unbounded"
    assert child.invoked_with is None, "a refused input must not start the child run"
