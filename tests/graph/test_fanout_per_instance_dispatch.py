"""When _stream_node is asked to run a synthesized id like worker[2],
it resolves the underlying _AgentNodeRef definition and renders the
input_template with fanout_index=2 + fanout_item in scope.

Builds a Begin -> FanOut(broadcast worker count=3) graph and asserts the
agent stub is invoked 3 times, with the rendered input carrying each
fanout_index AND the aggregator list at nodes.worker carrying the 3
NodeOutputs in index order."""

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
    NodeOutput,
    _AgentNodeRef,
    _BeginNode,
    _EndNode,
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
    """Echoes the rendered user text back via a TextDelta so the test can
    read each per-instance rendered template."""

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
async def test_per_instance_dispatch_renders_fanout_index() -> None:
    """Begin -> FanOut(broadcast worker count=3): each synthesized worker[i]
    instance runs the underlying ``worker`` agent with its input_template
    rendered against an extra scope carrying ``fanout_index=i``."""
    graph = Graph.model_construct(
        id="g-dispatch",
        description="Begin -> FanOut(broadcast)",
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
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="fan"),
        ],
        max_iterations=10,
        harness_id=None,
    )
    llm = _FakeLLM()
    executor, _thread, _ts = await _build_executor(graph=graph, llm=llm)

    # No End reachable through actual edges in this minimal fixture; the
    # executor runs all three worker instances then runs out of ready nodes
    # and exits cleanly (no terminal_reached / no any_failed).
    await _drain(executor.invoke([]))

    assert len(llm.calls) == 3
    rendered_texts: list[str] = []
    for call in llm.calls:
        last_user = next(
            (m for m in reversed(call["messages"]) if m.role == "user"),
            None,
        )
        assert last_user is not None
        for p in last_user.parts:
            if getattr(p, "type", None) == "text":
                rendered_texts.append(p.text)  # type: ignore[union-attr]
                break
    assert sorted(rendered_texts) == ["W0", "W1", "W2"]


@pytest.mark.asyncio
async def test_per_instance_dispatch_builds_aggregator_list() -> None:
    """After all worker[i] instances complete, ``context.nodes['worker']`` is
    a list[NodeOutput] in index order — one entry per instance.

    We capture the final GraphContext by tapping ``_compute_next_ready`` (the
    only hook that the outer loop awaits after applying per-node results); the
    aggregator list gets populated in the outer loop's result-application
    block just before ``_compute_next_ready`` fires."""
    graph = Graph.model_construct(
        id="g-aggregator",
        description="Begin -> FanOut(broadcast)",
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
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="fan"),
        ],
        max_iterations=10,
        harness_id=None,
    )
    llm = _FakeLLM()
    executor, _thread, _ts = await _build_executor(graph=graph, llm=llm)

    captured_nodes: list[dict[str, Any]] = []
    original_compute = executor._compute_next_ready

    async def tap_compute(just_ran, context):
        # context is the GraphContext mutated in place by the outer loop;
        # snapshot it before the conditional-edge walk that may further
        # mutate next_ready bookkeeping.
        captured_nodes.append(
            {
                k: (
                    "list"
                    if isinstance(v, list)
                    else getattr(v, "text", repr(v))
                )
                for k, v in context.nodes.items()
            }
        )
        return await original_compute(just_ran, context)

    executor._compute_next_ready = tap_compute  # type: ignore[method-assign]

    await _drain(executor.invoke([]))

    # The aggregator list at nodes['worker'] gets populated as each worker
    # instance completes; by the time the last superstep's results are
    # applied we must have seen the list show up.
    saw_aggregator = any(snap.get("worker") == "list" for snap in captured_nodes)
    assert saw_aggregator, (
        f"expected aggregator list at nodes['worker'] in some snapshot; "
        f"got {captured_nodes}"
    )
