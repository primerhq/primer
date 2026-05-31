"""FanOutSpec.on_failure='collect' — failed workers' outputs are stamped
with NodeOutput.error + ended_detail; a downstream FanIn can branch on
``n.error`` in its aggregate_template.

Begin -> FanOut(broadcast worker count=3, on_failure='collect')
      -> FanIn(aggregate_template branches on n.error) -> End

Worker[1] raises; workers 0/2 succeed. Asserts:

* Graph terminates ``completed`` (not ``failed``).
* ``context.nodes['worker[1]'].error`` and ``ended_detail`` are populated.
* FanIn.text reflects the failed/success mix via the template branching on
  ``n.error``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Generic, TypeVar

import pytest

from primer.graph.base import _GraphEndOutputEvent, _GraphErrorEvent
from primer.graph.executor import GraphExecutor
from primer.graph.router import RouterRegistry
from primer.model.agent import Agent, AgentModel
from primer.model.chat import (
    Done,
    Message,
    StreamEvent,
    TextDelta,
)
from primer.model.common import Identifiable
from primer.model.except_ import ConflictError, NotFoundError
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
)
from primer.model.provider import LLMModel
from primer.model.storage import (
    CursorPage,
    CursorPageResponse,
    FieldRef,
    OffsetPage,
    OffsetPageResponse,
    Op,
    PageRequest,
    Predicate,
    Value,
)


_T = TypeVar("_T", bound=Identifiable)


class _InMemoryStorage(Generic[_T]):
    def __init__(self, model_cls: type[_T]) -> None:
        self._cls = model_cls
        self._data: dict[str, _T] = {}

    async def get(self, id: str) -> _T | None:
        return self._data.get(id)

    async def create(self, entity: _T) -> _T:
        if entity.id in self._data:
            raise ConflictError(f"id {entity.id!r} already exists")
        self._data[entity.id] = entity
        return entity

    async def update(self, entity: _T) -> _T:
        if entity.id not in self._data:
            raise NotFoundError(f"no entity with id {entity.id!r}")
        self._data[entity.id] = entity
        return entity

    async def delete(self, id: str) -> None:
        if id not in self._data:
            raise NotFoundError(f"no entity with id {id!r}")
        del self._data[id]

    async def list(self, page: PageRequest, *, order_by=None):
        return await self.find(None, page, order_by=order_by)

    async def find(
        self,
        predicate: Predicate | None,
        page: PageRequest,
        *,
        order_by=None,
    ):
        items = list(self._data.values())
        if predicate is not None:
            items = [i for i in items if self._eval(predicate, i)]
        if order_by:
            for ob in reversed(order_by):
                items.sort(
                    key=lambda x, f=ob.field: getattr(x, f),
                    reverse=(ob.direction == "desc"),
                )
        if isinstance(page, OffsetPage):
            sliced = items[page.offset : page.offset + page.length]
            return OffsetPageResponse(
                offset=page.offset,
                length=len(sliced),
                total=len(items),
                items=sliced,
            )
        offset = int(page.cursor) if page.cursor else 0
        sliced = items[offset : offset + page.length]
        next_cursor: str | None = None
        if offset + page.length < len(items):
            next_cursor = str(offset + page.length)
        return CursorPageResponse(next_cursor=next_cursor, items=sliced)

    @staticmethod
    def _eval(p: Predicate, entity) -> bool:
        if p.op == Op.EQ:
            assert isinstance(p.left, FieldRef) and isinstance(p.right, Value)
            return getattr(entity, p.left.name) == p.right.value
        if p.op == Op.AND:
            assert isinstance(p.left, Predicate) and isinstance(p.right, Predicate)
            return _InMemoryStorage._eval(p.left, entity) and _InMemoryStorage._eval(
                p.right, entity
            )
        raise NotImplementedError(f"op {p.op!r} not supported")


class _FailingFakeLLM:
    """LLM stub: raises for prompts containing ``fail_marker``; otherwise
    echoes the last user-message's text back as a single TextDelta."""

    def __init__(self, fail_marker: str) -> None:
        self._fail_marker = fail_marker
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
        if self._fail_marker in text:
            return self._stream_fail(text)
        return self._stream_ok(text)

    async def _stream_ok(self, text: str) -> AsyncIterator[StreamEvent]:
        yield TextDelta(text=text, index=0)
        yield Done(stop_reason="stop", raw_reason="stop")

    async def _stream_fail(self, text: str) -> AsyncIterator[StreamEvent]:
        if False:
            yield  # pragma: no cover
        raise RuntimeError(f"simulated worker failure for prompt={text!r}")


def _agent(agent_id: str) -> Agent:
    return Agent(
        id=agent_id,
        description=f"agent {agent_id}",
        model=AgentModel(provider_id="p", model_name="m"),
        system_prompt=[],
    )


def _model() -> LLMModel:
    return LLMModel(name="m", context_length=128_000)


async def _build_executor(*, graph: Graph, llm: _FailingFakeLLM):
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
        title="t",
    )
    executor = GraphExecutor(
        graph=graph,
        agent_resolver=agent_resolver,
        llm_resolver=llm_resolver,  # type: ignore[arg-type]
        thread_storage=thread_storage,  # type: ignore[arg-type]
        message_storage=message_storage,  # type: ignore[arg-type]
        graph_thread_id=thread.id,
        router_registry=RouterRegistry(),
    )
    return executor, thread, thread_storage


async def _drain(it):
    return [ev async for ev in it]


@pytest.mark.asyncio
async def test_collect_stamps_node_error_and_lets_fanin_branch() -> None:
    """Begin -> FanOut(broadcast count=3, on_failure='collect') -> FanIn -> End.

    Worker[1] raises; workers 0/2 succeed. FanIn's aggregate_template
    branches on ``n.error`` to emit ``E`` per failed worker and the worker's
    text per successful one. Graph terminates ``completed``.
    """
    graph = Graph.model_construct(
        id="g-collect",
        description="Begin -> FanOut(broadcast collect) -> FanIn -> End",
        nodes=[
            _BeginNode(id="begin"),
            _FanOutNode(
                id="fan",
                specs=[
                    FanOutSpec(
                        kind="broadcast",
                        target_node_id="worker",
                        count=3,
                        on_failure="collect",
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
                    "{% for n in nodes.worker %}"
                    "{% if n.error %}E{% else %}{{ n.text }}{% endif %}"
                    "{% endfor %}"
                ),
            ),
            _EndNode(id="end", output_template="{{ nodes.agg.text }}"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="fan"),
            _StaticEdge(from_node="worker", to_node="agg"),
            _StaticEdge(from_node="agg", to_node="end"),
        ],
        max_iterations=10,
        harness_id=None,
    )
    llm = _FailingFakeLLM(fail_marker="W1")
    executor, thread, thread_storage = await _build_executor(graph=graph, llm=llm)

    events = await _drain(executor.invoke([]))

    # Graph completed (collect mode does NOT terminate failed).
    final = await thread_storage.get(thread.id)
    assert final is not None
    assert final.ended_reason == "completed", (
        f"expected completed; got {final.ended_reason!r} / {final.ended_detail!r}"
    )

    # All 3 workers were invoked (worker[1] raised, but workers 0/2 succeeded).
    assert len(llm.calls) == 3

    # No terminal error event was emitted.
    err_events = [ev for ev in events if isinstance(ev, _GraphErrorEvent)]
    assert err_events == []

    # The End's output_template renders nodes.agg.text. Grab the End event.
    end_events = [ev for ev in events if isinstance(ev, _GraphEndOutputEvent)]
    assert end_events, "expected at least one _GraphEndOutputEvent"
    end_text = end_events[-1].text
    # FanIn aggregate: worker[0]='W0', worker[1]='E', worker[2]='W2'
    assert end_text == "W0EW2", f"unexpected aggregate text: {end_text!r}"


@pytest.mark.asyncio
async def test_collect_stamps_node_output_error_field() -> None:
    """The failed worker's NodeOutput in context.nodes carries both
    ``error`` and ``ended_detail`` (Spec B §2.5 NodeOutput.error path).
    """
    graph = Graph.model_construct(
        id="g-collect-stamp",
        description="Begin -> FanOut(broadcast collect) -> FanIn -> End",
        nodes=[
            _BeginNode(id="begin"),
            _FanOutNode(
                id="fan",
                specs=[
                    FanOutSpec(
                        kind="broadcast",
                        target_node_id="worker",
                        count=3,
                        on_failure="collect",
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
                aggregate_template="ok",
            ),
            _EndNode(id="end", output_template="{{ nodes.agg.text }}"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="fan"),
            _StaticEdge(from_node="worker", to_node="agg"),
            _StaticEdge(from_node="agg", to_node="end"),
        ],
        max_iterations=10,
        harness_id=None,
    )
    llm = _FailingFakeLLM(fail_marker="W1")
    executor, thread, thread_storage = await _build_executor(graph=graph, llm=llm)

    # Tap the FanIn's _stream_node entry by patching aggregate_template
    # rendering — but simpler: snapshot context.nodes via render_template_safely
    # callback. The cleanest hook is to patch _render_fanin_output's input,
    # which is too invasive. Instead: tap _compute_next_ready (called after
    # every superstep) and capture the GraphContext.nodes snapshot.
    captured_contexts: list[dict[str, Any]] = []
    original_compute = executor._compute_next_ready

    async def tap_compute(just_ran, context):
        captured_contexts.append(dict(context.nodes))
        return await original_compute(just_ran, context)

    executor._compute_next_ready = tap_compute  # type: ignore[method-assign]

    await _drain(executor.invoke([]))

    # Find a snapshot that contains worker[1] as a NodeOutput with error stamped.
    fail_node = None
    for snap in captured_contexts:
        node = snap.get("worker[1]")
        if isinstance(node, NodeOutput) and node.error is not None:
            fail_node = node
            break
    assert fail_node is not None, (
        f"expected worker[1] with stamped error in some snapshot; "
        f"snapshots had keys={[set(s.keys()) for s in captured_contexts]}"
    )
    assert fail_node.error is not None
    assert fail_node.ended_detail is not None
    assert "simulated worker failure" in fail_node.error
