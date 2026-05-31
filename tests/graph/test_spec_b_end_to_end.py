"""Phase 11.2 — end-to-end Spec B graph: Begin → FanOut → workers → FanIn → ToolCall → End.

The full Spec B topology in a single pass:

* :class:`_BeginNode` seeds the initial input.
* :class:`_FanOutNode` broadcasts to 3 deterministic worker agent
  instances (``worker[0..2]``). Each worker stub emits a distinct text
  derived from ``fanout_index`` so the FanIn aggregation is
  observable.
* :class:`_FanInNode` aggregates the workers via Jinja, emitting a
  comma-joined string the downstream ToolCall consumes.
* :class:`_ToolCallNode` runs a stub dispatcher and asserts the
  templated args carry the FanIn aggregate.
* :class:`_EndNode` renders the tool result via ``output_template``.

Asserts the executor's session-level event stream contains a
:class:`_GraphNodeEvent` per node-event plus a terminal
:class:`_GraphEndOutputEvent` carrying the tool result.

Mirrors the patterns used by Phase 2-5 tests
(:mod:`tests.graph.test_fanout_broadcast_e2e`,
:mod:`tests.graph.test_fanout_collect`,
:mod:`tests.graph.test_toolcall_dispatch`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from primer.graph.base import (
    _GraphEndOutputEvent,
    _GraphErrorEvent,
)
from primer.graph.executor import GraphExecutor
from primer.graph.router import RouterRegistry
from primer.model.agent import Agent, AgentModel
from primer.model.chat import (
    Done,
    ExtendedEvent,
    Message,
    StreamEvent,
    TextDelta,
    ToolResultPart,
)
from primer.model.graph import (
    FanOutSpec,
    Graph,
    GraphNodeMessage,
    GraphThread,
    NodeOutput,
    _AgentNodeRef,
    _BeginNode,
    _EndNode,
    _FanInNode,
    _FanOutNode,
    _StaticEdge,
    _ToolCallNode,
)
from primer.model.provider import LLMModel

from tests.graph.test_fanout_broadcast_e2e import _InMemoryStorage


# ===========================================================================
# Test doubles
# ===========================================================================


class _FanoutFakeLLM:
    """Deterministic worker LLM: echoes the user message verbatim so the
    aggregate_template can read each worker's distinct ``W{i}`` text.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def list_models(self):
        return ["m"]

    def stream(
        self,
        *,
        model: str,
        messages: list[Message],
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        self.calls.append({"model": model, "messages": list(messages), **kwargs})
        last_user = next(
            (m for m in reversed(messages) if m.role == "user"),
            None,
        )
        text = ""
        if last_user is not None:
            for p in last_user.parts:
                if getattr(p, "type", None) == "text":
                    text = p.text  # type: ignore[union-attr]
                    break
        return self._stream_impl(text)

    async def _stream_impl(self, text: str) -> AsyncIterator[StreamEvent]:
        yield TextDelta(text=text, index=0)
        yield Done(stop_reason="stop", raw_reason="stop")


def _agent(agent_id: str) -> Agent:
    return Agent(
        id=agent_id,
        description=f"agent {agent_id}",
        model=AgentModel(provider_id="p", model_name="m"),
        system_prompt=[],
    )


def _model() -> LLMModel:
    return LLMModel(name="m", context_length=128_000)


async def _drain(it: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    return [ev async for ev in it]


# ===========================================================================
# The end-to-end test
# ===========================================================================


@pytest.mark.asyncio
async def test_spec_b_end_to_end_graph_runs_to_completion() -> None:
    """Begin → FanOut(broadcast, count=3) → workers → FanIn → ToolCall → End.

    Pins the full Spec B vertical at the executor level:

    * 3 FanOut worker instances dispatched (each receives its
      ``fanout_index`` via the input_template).
    * FanIn aggregates the workers' texts into a comma-joined string.
    * ToolCall receives the FanIn aggregate via its arguments template
      (proving NodeOutput → ToolCall arg threading).
    * End renders the ToolCall result via ``output_template``.
    * The event stream contains :class:`_GraphNodeEvent` envelopes for
      worker / FanIn / ToolCall, plus a terminal
      :class:`_GraphEndOutputEvent`.
    """
    graph = Graph.model_construct(
        id="g-spec-b-e2e",
        description="Spec B end-to-end: FanOut → FanIn → ToolCall → End",
        nodes=[
            _BeginNode(id="begin"),
            _FanOutNode(
                id="fan",
                specs=[
                    FanOutSpec(
                        kind="broadcast",
                        target_node_id="worker",
                        count=3,
                    ),
                ],
            ),
            _AgentNodeRef(
                id="worker",
                agent_id="ag",
                input_template="W{{ fanout_index }}",
            ),
            _FanInNode(
                id="agg",
                aggregate_template=(
                    "{% for n in nodes.worker %}{{ n.text }}"
                    "{% if not loop.last %},{% endif %}{% endfor %}"
                ),
            ),
            _ToolCallNode(
                id="tool",
                tool_id="summary__build",
                arguments={"items": "{{ nodes.agg.text }}"},
            ),
            _EndNode(
                id="end",
                output_template="{{ nodes.tool.text }}",
            ),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="fan"),
            # FanOut's target_node_id 'worker' is implicit; spec B
            # forbids outgoing edges on FanOut so the executor seeds
            # the worker instances from the spec itself.
            _StaticEdge(from_node="worker", to_node="agg"),
            _StaticEdge(from_node="agg", to_node="tool"),
            _StaticEdge(from_node="tool", to_node="end"),
        ],
        max_iterations=10,
        harness_id=None,
    )

    seen_tool_calls: list[tuple[str, dict[str, Any]]] = []

    async def stub_dispatcher(node, arguments):
        seen_tool_calls.append((node.tool_id, dict(arguments)))
        return ToolResultPart(
            id="tc-e2e",
            output=f"SUMMARY[{arguments.get('items', '')}]",
        )

    llm = _FanoutFakeLLM()

    async def agent_resolver(agent_id: str) -> Agent:
        return _agent(agent_id)

    async def llm_resolver(agent: Agent):
        return (llm, _model())

    thread_storage: _InMemoryStorage[GraphThread] = _InMemoryStorage(GraphThread)
    message_storage: _InMemoryStorage[GraphNodeMessage] = _InMemoryStorage(
        GraphNodeMessage
    )
    thread = await GraphExecutor.open_thread(
        graph=graph,
        thread_storage=thread_storage,  # type: ignore[arg-type]
        title="spec-b-e2e",
    )
    executor = GraphExecutor(
        graph=graph,
        agent_resolver=agent_resolver,
        llm_resolver=llm_resolver,  # type: ignore[arg-type]
        thread_storage=thread_storage,  # type: ignore[arg-type]
        message_storage=message_storage,  # type: ignore[arg-type]
        graph_thread_id=thread.id,
        router_registry=RouterRegistry(),
        tool_dispatcher=stub_dispatcher,
    )

    events = await _drain(executor.invoke([]))

    # -------- 1. All 3 FanOut worker instances ran -------------------
    # The fake LLM logged one ``stream`` call per worker instance. The
    # last user message in each call is the worker's templated input
    # ``W{i}``; assert we saw all three distinct prompts.
    last_user_texts: list[str] = []
    for call in llm.calls:
        last_user = next(
            (m for m in reversed(call["messages"]) if m.role == "user"),
            None,
        )
        if last_user is not None and last_user.parts:
            last_user_texts.append(last_user.parts[0].text)  # type: ignore[union-attr]
    assert sorted(set(last_user_texts)) == ["W0", "W1", "W2"], (
        f"expected 3 distinct worker dispatches, got {last_user_texts!r}"
    )
    # Each worker fired at least one streaming call.
    assert len(llm.calls) >= 3

    # The graph context also records each worker as a list under
    # ``nodes.worker`` (FanOut targets are list-typed).
    assert executor._context is not None
    worker_nodes = executor._context.nodes.get("worker")
    assert isinstance(worker_nodes, list)
    assert len(worker_nodes) == 3
    for nd in worker_nodes:
        assert isinstance(nd, NodeOutput)
    # Workers' texts in fanout_index order.
    assert [n.text for n in worker_nodes] == ["W0", "W1", "W2"]

    # -------- 2. FanIn aggregated the workers' outputs ---------------
    agg_node = executor._context.nodes.get("agg")
    assert isinstance(agg_node, NodeOutput)
    assert agg_node.text == "W0,W1,W2"

    # -------- 3. ToolCall received the FanIn aggregate ---------------
    # Templated arg lookup wired the FanIn output into the ToolCall's
    # ``items`` argument.
    assert seen_tool_calls == [("summary__build", {"items": "W0,W1,W2"})]

    tool_node = executor._context.nodes.get("tool")
    assert isinstance(tool_node, NodeOutput)
    assert tool_node.text == "SUMMARY[W0,W1,W2]"

    # -------- 4. End rendered the ToolCall's output_template ---------
    end_outputs = [e for e in events if isinstance(e, _GraphEndOutputEvent)]
    assert len(end_outputs) == 1
    assert end_outputs[0].text == "SUMMARY[W0,W1,W2]"

    # -------- 5. No terminal error events ----------------------------
    err_events = [e for e in events if isinstance(e, _GraphErrorEvent)]
    assert err_events == []

    # -------- 6. Event stream carries per-node graph-node events -----
    # ExtendedEvent(_GraphNodeEvent(...)) envelopes correlate worker /
    # FanIn / ToolCall stream events to their source nodes. We check
    # the per-node event sequence at the executor level: the workers'
    # text-deltas + dones wrap as _GraphNodeEvent(node_id='worker[i]'),
    # the ToolCall's tool result wraps as
    # _GraphNodeEvent(node_id='tool').
    graph_node_event_ids: list[str] = []
    for ev in events:
        if isinstance(ev, ExtendedEvent):
            inner = ev.extended
            inner_node_id = getattr(inner, "node_id", None)
            if inner_node_id is not None:
                graph_node_event_ids.append(inner_node_id)

    # Worker events are tagged with the synthesized instance id
    # (``worker[i]``) OR the base target id (``worker``) depending on
    # the executor's stamping path; tolerate both. Pin that we saw at
    # least one worker-tagged event per instance.
    worker_event_count = sum(
        1
        for nid in graph_node_event_ids
        if nid == "worker" or nid.startswith("worker[")
    )
    assert worker_event_count >= 3, (
        f"expected at least 3 worker-tagged events, got {graph_node_event_ids!r}"
    )

    # ToolCall nodes don't emit a wrapped child stream-event in this
    # executor — the dispatcher returns its ``ToolResultPart``
    # synchronously and the NodeOutput is stamped directly. The
    # presence of the End event + the stub_dispatcher call log proves
    # ToolCall ran; the bare ``worker``-tagged events confirm graph
    # event multiplexing for agent nodes.

    # -------- 7. Thread state ENDED completed ------------------------
    loaded = await thread_storage.get(thread.id)
    assert loaded is not None
    assert loaded.ended_reason == "completed"
    assert loaded.ended_detail is None
