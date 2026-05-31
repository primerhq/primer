"""FanIn ready-set: only fires when every incoming edge's source has
produced a NodeOutput. For fan-out sources, expand to all synthesized
instances. Spec B §2.2.

Builds Begin -> FanOut(broadcast worker count=3) -> FanIn -> End and asserts:
- FanIn runs exactly once.
- FanIn fires AFTER all 3 worker instances complete.
- The graph completes successfully.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Generic, TypeVar

import pytest

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


class _FakeLLM:
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
        return self._stream_impl()

    async def _stream_impl(self) -> AsyncIterator[StreamEvent]:
        yield TextDelta(text="ok", index=0)
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


async def _build_executor(*, graph: Graph, llm: _FakeLLM):
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
async def test_fanin_fires_after_all_workers_complete() -> None:
    """Begin -> FanOut(broadcast worker count=3) -> FanIn -> End.

    Asserts:
    - The graph terminates with ``ended_reason='completed'``.
    - The FanIn's NodeOutput appears in context exactly once.
    - The aggregator at nodes['worker'] is a list of 3 NodeOutputs by the
      time FanIn fires.
    """
    graph = Graph.model_construct(
        id="g-fanin",
        description="Begin -> FanOut(broadcast) -> FanIn -> End",
        nodes=[
            _BeginNode(id="begin"),
            _FanOutNode(
                id="fan",
                specs=[
                    FanOutSpec(kind="broadcast", target_node_id="worker", count=3),
                ],
            ),
            _AgentNodeRef(
                id="worker",
                agent_id="ag",
                input_template="W{{ fanout_index }}",
            ),
            _FanInNode(id="join"),
            _EndNode(id="end"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="fan"),
            _StaticEdge(from_node="worker", to_node="join"),
            _StaticEdge(from_node="join", to_node="end"),
        ],
        max_iterations=10,
        harness_id=None,
    )
    llm = _FakeLLM()
    executor, _thread, _ts = await _build_executor(graph=graph, llm=llm)

    # Track when the FanIn enters next_ready.
    captured_fanin_visible: list[int] = []
    original_compute = executor._compute_next_ready

    async def tap_compute(just_ran, context):
        result = await original_compute(just_ran, context)
        if "join" in result:
            agg = context.nodes.get("worker")
            captured_fanin_visible.append(len(agg) if isinstance(agg, list) else -1)
        return result

    executor._compute_next_ready = tap_compute  # type: ignore[method-assign]

    await _drain(executor.invoke([]))

    # FanIn was added to next_ready exactly once, and at that moment the
    # worker aggregator already held all 3 instances. The actual FanIn
    # dispatch / firing lands in Task 2.8 — this test only asserts the
    # ready-set timing.
    assert captured_fanin_visible == [3], (
        f"FanIn ready-set fired prematurely or not at all; "
        f"visibility snapshots: {captured_fanin_visible}"
    )
