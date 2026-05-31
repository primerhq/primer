"""End termination + ended_detail propagation through the executor loop.

Builds a minimal Begin→End graph (no agent nodes) and exercises the
storage-backed executor. The happy path ends ``completed``; the failure
path (End with a schema-violating output_template) ends ``failed`` and
carries ``ended_detail='end_output_invalid'``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Generic, TypeVar

import pytest

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
    """Bare-minimum :class:`Storage` test double (mirrors test_executor.py)."""

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


async def _drain(it: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
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
async def test_begin_to_end_happy_path_completes() -> None:
    graph = Graph(
        id="g-be-happy",
        description="Begin -> End",
        entry_node_id="b",
        nodes=[_BeginNode(id="b"), _EndNode(id="e")],
        edges=[_StaticEdge(from_node="b", to_node="e")],
    )
    executor, thread, ts = await _build_executor(graph=graph)
    await _drain(executor.invoke([]))
    loaded = await ts.get(thread.id)
    assert loaded is not None
    assert loaded.status == SessionStatus.ENDED
    assert loaded.ended_reason == "completed"
    assert loaded.ended_detail is None


@pytest.mark.asyncio
async def test_begin_to_end_with_bad_schema_carries_ended_detail() -> None:
    graph = Graph(
        id="g-be-bad",
        description="Begin -> End (schema mismatch)",
        entry_node_id="b",
        nodes=[
            _BeginNode(id="b"),
            _EndNode(
                id="e",
                output_template="not json at all",
                output_schema={"type": "object"},
            ),
        ],
        edges=[_StaticEdge(from_node="b", to_node="e")],
    )
    executor, thread, ts = await _build_executor(graph=graph)
    await _drain(executor.invoke([]))
    loaded = await ts.get(thread.id)
    assert loaded is not None
    assert loaded.status == SessionStatus.ENDED
    assert loaded.ended_reason == "failed"
    assert loaded.ended_detail == "end_output_invalid"
