"""Failure paths emit a terminal :class:`_GraphErrorEvent` with the
spec §5.4 ``code``/``node_id`` payload before the graph terminates."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Generic, TypeVar

import pytest

from primer.graph.base import _GraphErrorEvent
from primer.graph.executor import GraphExecutor
from primer.model.chat import Message, StreamEvent
from primer.model.common import Identifiable
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.graph import (
    Graph,
    GraphNodeMessage,
    GraphThread,
    _BeginNode,
    _EndNode,
    _StaticEdge,
)
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


async def _drain(it: AsyncIterator) -> list:
    return [ev async for ev in it]


async def _build_executor(*, graph: Graph):
    async def agent_resolver(_agent_id: str):
        raise KeyError("no agents in this graph")

    async def llm_resolver(_agent):
        raise KeyError("no LLM in this graph")

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
    return executor, thread, thread_storage


@pytest.mark.asyncio
async def test_emits_graph_error_event_on_end_output_invalid() -> None:
    graph = Graph(
        id="g-err-emit",
        description="Begin -> End (bad schema)",
        entry_node_id="b",
        nodes=[
            _BeginNode(id="b"),
            _EndNode(
                id="e",
                output_template="plain text",
                output_schema={"type": "object"},
            ),
        ],
        edges=[_StaticEdge(from_node="b", to_node="e")],
    )
    executor, _thread, _ts = await _build_executor(graph=graph)
    events = await _drain(executor.invoke([]))
    err_events = [ev for ev in events if isinstance(ev, _GraphErrorEvent)]
    assert err_events, "expected at least one _GraphErrorEvent"
    assert err_events[0].code == "end_output_invalid"
    assert err_events[0].node_id == "e"
