"""When ``max_iterations`` is hit, the executor ends ``failed`` with
``ended_detail='max_iterations_exceeded'`` and emits a terminal
:class:`_GraphErrorEvent`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Generic, TypeVar

import pytest

from primer.graph.base import _GraphErrorEvent
from primer.graph.executor import GraphExecutor
from primer.model.agent import Agent, AgentModel
from primer.model.chat import Done, Message, StreamEvent, TextDelta
from primer.model.common import Identifiable
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.graph import (
    BranchCondition,
    Graph,
    GraphNodeMessage,
    GraphThread,
    JsonPathBranch,
    _AgentNodeRef,
    _BeginNode,
    _ConditionalEdge,
    _JsonPathRouter,
    _StaticEdge,
)
from primer.model.provider import LLMModel
from primer.model.workspace_session import SessionStatus
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

    async def list(self, page, *, order_by=None):
        return await self.find(None, page, order_by=order_by)

    async def find(self, predicate, page, *, order_by=None):
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
        next_cursor = None
        if offset + page.length < len(items):
            next_cursor = str(offset + page.length)
        return CursorPageResponse(next_cursor=next_cursor, items=sliced)

    @staticmethod
    def _eval(p: Predicate, entity) -> bool:
        if p.op == Op.EQ:
            return getattr(entity, p.left.name) == p.right.value
        if p.op == Op.AND:
            return _InMemoryStorage._eval(
                p.left, entity
            ) and _InMemoryStorage._eval(p.right, entity)
        raise NotImplementedError(f"op {p.op!r} not supported")


class _FakeLLM:
    def __init__(self, *, scripts: list[list[StreamEvent]]) -> None:
        self._scripts = scripts
        self._cursor = 0
        self.calls: list[dict[str, Any]] = []

    async def list_models(self):
        return ["m"]

    def stream(self, *, model: str, messages: list[Message], **kwargs: Any):
        self.calls.append({"model": model, "messages": list(messages), **kwargs})
        idx = min(self._cursor, len(self._scripts) - 1)
        self._cursor += 1
        return self._stream_impl(self._scripts[idx])

    async def _stream_impl(self, events):
        for ev in events:
            yield ev


def _agent(agent_id: str) -> Agent:
    return Agent(
        id=agent_id,
        description=f"agent {agent_id}",
        model=AgentModel(provider_id="p", model_name="m"),
        system_prompt=[],
    )


def _model() -> LLMModel:
    return LLMModel(name="m", context_length=128_000)


@pytest.mark.asyncio
async def test_max_iterations_exceeded_ended_detail() -> None:
    """Build a Begin -> Agent -> (loop-back to Agent) graph with
    ``max_iterations=2``; the agent's response always routes back, so the
    cycle bound trips on iteration 2. Assert the executor ends ``failed``
    with ``ended_detail='max_iterations_exceeded'`` and yields a terminal
    ``_GraphErrorEvent``."""
    graph = Graph(
        id="g-max-iter",
        description="Begin -> A -> A (forever, bounded)",
        entry_node_id="b",
        max_iterations=2,
        nodes=[
            _BeginNode(id="b"),
            _AgentNodeRef(
                id="a",
                agent_id="x",
                response_format={"type": "object"},
            ),
        ],
        edges=[
            _StaticEdge(from_node="b", to_node="a"),
            _ConditionalEdge(
                from_node="a",
                router=_JsonPathRouter(
                    branches=[
                        JsonPathBranch(
                            conditions=[
                                BranchCondition(path="go", op="eq", value="a")
                            ],
                            to_node="a",
                        )
                    ],
                ),
            ),
        ],
    )
    llm = _FakeLLM(
        scripts=[
            [
                TextDelta(text='{"go": "a"}', index=0),
                Done(stop_reason="stop", raw_reason="stop"),
            ]
        ]
    )

    async def agent_resolver(agent_id: str):
        return _agent(agent_id) if agent_id == "x" else None

    async def llm_resolver(_agent: Agent):
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
    )

    events = [ev async for ev in executor.invoke([])]
    err_events = [ev for ev in events if isinstance(ev, _GraphErrorEvent)]
    assert err_events, "expected at least one _GraphErrorEvent"
    assert err_events[0].code == "max_iterations_exceeded"

    loaded = await thread_storage.get(thread.id)
    assert loaded is not None
    assert loaded.status == SessionStatus.ENDED
    assert loaded.ended_reason == "failed"
    assert loaded.ended_detail == "max_iterations_exceeded"
