"""End-to-end FanOut map: Begin -> planner -> FanOut(map source=planner.topics)
-> worker. Each worker instance receives the matching list element as
``fanout_item`` and runs once."""

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


class _ScriptedLLM:
    """LLM that returns the next script line per call.

    Each call consumes the next ``scripts[idx]`` entry (a fixed string)
    emitted as a single TextDelta + Done."""

    def __init__(self, scripts: list[str]) -> None:
        self._scripts = scripts
        self._cursor = 0
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
        idx = min(self._cursor, len(self._scripts) - 1)
        self._cursor += 1
        return self._stream_impl(self._scripts[idx])

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


async def _build_executor(*, graph: Graph, llm: _ScriptedLLM):
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
async def test_map_spawns_one_instance_per_source_item() -> None:
    """Begin -> planner({topics:[a,b,c]}) -> FanOut(map planner.topics -> worker).

    Asserts:
    - The worker agent is invoked 3 times (one per planner topic).
    - Each invocation's rendered template includes the matching topic
      (carried via ``fanout_item``).
    """
    graph = Graph.model_construct(
        id="g-map",
        description="Begin -> planner -> FanOut(map) -> worker",
        nodes=[
            _BeginNode(id="begin"),
            _AgentNodeRef(
                id="planner",
                agent_id="ag-planner",
                response_format={
                    "type": "object",
                    "required": ["topics"],
                    "properties": {
                        "topics": {"type": "array", "items": {"type": "string"}}
                    },
                },
            ),
            _FanOutNode(
                id="fan",
                specs=[
                    FanOutSpec(
                        kind="map",
                        target_node_id="worker",
                        source_node_id="planner",
                        source_path="topics",
                    ),
                ],
            ),
            _AgentNodeRef(
                id="worker",
                agent_id="ag-worker",
                input_template="Topic: {{ fanout_item }} (idx={{ fanout_index }})",
            ),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="planner"),
            _StaticEdge(from_node="planner", to_node="fan"),
        ],
        max_iterations=10,
        harness_id=None,
    )
    # Planner returns its parsed JSON; workers each return a short reply.
    llm = _ScriptedLLM(
        scripts=[
            '{"topics": ["alpha", "beta", "gamma"]}',  # planner
            "ack-alpha",  # worker[0]
            "ack-beta",  # worker[1]
            "ack-gamma",  # worker[2]
        ]
    )
    executor, _thread, _ts = await _build_executor(graph=graph, llm=llm)
    await _drain(executor.invoke([]))

    # planner + 3 worker instances.
    assert len(llm.calls) == 4

    # Collect rendered worker inputs.
    worker_inputs: list[str] = []
    for call in llm.calls[1:]:
        last_user = next(
            (m for m in reversed(call["messages"]) if m.role == "user"),
            None,
        )
        assert last_user is not None
        for p in last_user.parts:
            if getattr(p, "type", None) == "text":
                worker_inputs.append(p.text)  # type: ignore[union-attr]
                break

    assert sorted(worker_inputs) == [
        "Topic: alpha (idx=0)",
        "Topic: beta (idx=1)",
        "Topic: gamma (idx=2)",
    ]
