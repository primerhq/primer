"""Verifies StorageGraphExecutor emits TurnLogRecord rows for per-node
+ graph-level events.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Generic, TypeVar

import pytest

from primer.graph.executor import GraphExecutor
from primer.model.agent import Agent, AgentModel
from primer.model.chat import Done, Message, StreamEvent, TextDelta
from primer.model.common import Identifiable
from primer.model.except_ import ConflictError, NetworkError, NotFoundError
from primer.model.graph import (
    Graph,
    GraphNodeMessage,
    GraphThread,
    _AgentNodeRef,
    _BeginNode,
    _EndNode,
    _StaticEdge,
)
from primer.model.provider import LLMModel
from primer.model.storage import (
    CursorPage,
    CursorPageResponse,
    OffsetPage,
    OffsetPageResponse,
    PageRequest,
    Predicate,
)
from primer.model.turn_log import TurnLogKind, TurnLogRecord


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

    async def find(self, predicate, page, *, order_by=None):
        items = list(self._data.values())
        if isinstance(page, OffsetPage):
            sliced = items[page.offset:page.offset + page.length]
            return OffsetPageResponse(
                offset=page.offset, length=len(sliced),
                total=len(items), items=sliced,
            )
        return CursorPageResponse(next_cursor=None, items=items)


class _FakeLLM:
    def __init__(self, *, scripts: list[list[StreamEvent]]) -> None:
        self._scripts = scripts
        self._cursor = 0

    async def list_models(self):
        return ["m"]

    def stream(self, *, model, messages, **kwargs):
        idx = min(self._cursor, len(self._scripts) - 1)
        self._cursor += 1
        return self._impl(self._scripts[idx])

    async def _impl(self, events):
        for ev in events:
            yield ev


def _agent(agent_id: str) -> Agent:
    return Agent(
        id=agent_id,
        description=f"agent {agent_id}",
        model=AgentModel(provider_id="p", model_name="m"),
    )


def _model() -> LLMModel:
    return LLMModel(name="m", context_length=128_000)


async def _build(
    *, graph: Graph, llm, agents,
    turn_log_storage,
):
    async def agent_resolver(aid):
        return agents[aid]

    async def llm_resolver(agent):
        return (llm, _model())

    thread_storage: _InMemoryStorage[GraphThread] = _InMemoryStorage(GraphThread)
    message_storage: _InMemoryStorage[GraphNodeMessage] = _InMemoryStorage(
        GraphNodeMessage,
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
        turn_log_storage=turn_log_storage,  # type: ignore[arg-type]
    )
    return executor, thread


async def _drain(it: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    return [ev async for ev in it]


@pytest.mark.asyncio
async def test_per_node_started_completed_lands_as_storage_rows():
    graph = Graph(
        id="g",
        description="A -> exit",
        nodes=[
            _BeginNode(id="begin"),
            _AgentNodeRef(id="A", agent_id="x"),
            _EndNode(id="exit"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="A"),
            _StaticEdge(from_node="A", to_node="exit"),
        ],
    )
    llm = _FakeLLM(scripts=[[
        TextDelta(text="hi", index=0),
        Done(stop_reason="stop", raw_reason="stop"),
    ]])
    turn_log_storage: _InMemoryStorage[TurnLogRecord] = _InMemoryStorage(
        TurnLogRecord,
    )
    executor, thread = await _build(
        graph=graph, llm=llm, agents={"x": _agent("x")},
        turn_log_storage=turn_log_storage,
    )
    await _drain(executor.invoke([]))

    rows = list(turn_log_storage._data.values())
    assert len(rows) > 0
    kinds = [r.kind for r in rows]
    assert TurnLogKind.STARTED in kinds
    assert TurnLogKind.COMPLETED in kinds
    assert TurnLogKind.SUPERSTEP_STARTED in kinds
    assert TurnLogKind.SUPERSTEP_ENDED in kinds

    # Per-node rows carry node_id, graph-level rows carry node_id=None.
    node_started = [r for r in rows if r.kind == TurnLogKind.STARTED]
    assert all(r.node_id is not None for r in node_started)
    ss_started = [r for r in rows if r.kind == TurnLogKind.SUPERSTEP_STARTED]
    assert all(r.node_id is None for r in ss_started)

    # run_id is the GraphThread id for every row.
    assert all(r.run_id == thread.id for r in rows)


@pytest.mark.asyncio
async def test_node_failed_persists_problem_details_payload():
    graph = Graph(
        id="g-fail",
        description="A -> exit",
        nodes=[
            _BeginNode(id="begin"),
            _AgentNodeRef(id="A", agent_id="x"),
            _EndNode(id="exit"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="A"),
            _StaticEdge(from_node="A", to_node="exit"),
        ],
    )

    class _BrokenLLM:
        async def list_models(self):
            return ["m"]

        def stream(self, *, model, messages, **kwargs):
            return self._impl()

        async def _impl(self):
            raise NetworkError("upstream gone")
            yield  # pragma: no cover

    turn_log_storage: _InMemoryStorage[TurnLogRecord] = _InMemoryStorage(
        TurnLogRecord,
    )
    executor, thread = await _build(
        graph=graph, llm=_BrokenLLM(),
        agents={"x": _agent("x")},
        turn_log_storage=turn_log_storage,
    )
    await _drain(executor.invoke([]))

    rows = list(turn_log_storage._data.values())
    failed_rows = [r for r in rows if r.kind == TurnLogKind.FAILED]
    assert len(failed_rows) >= 1
    f = failed_rows[0]
    assert f.node_id == "A"
    assert isinstance(f.payload, dict)
    # payload carries the failed event's non-base fields, including `error`
    # (a ProblemDetails-shaped dict) and `duration_ms`.
    assert "error" in f.payload
    err = f.payload["error"]
    assert isinstance(err, dict)
    assert "status" in err
    assert "title" in err


@pytest.mark.asyncio
async def test_no_turn_log_storage_keeps_executor_silent():
    """Backwards compat: when ``turn_log_storage`` is not passed, the
    executor still runs normally and writes zero rows."""
    graph = Graph(
        id="g",
        description="A -> exit",
        nodes=[
            _BeginNode(id="begin"),
            _AgentNodeRef(id="A", agent_id="x"),
            _EndNode(id="exit"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="A"),
            _StaticEdge(from_node="A", to_node="exit"),
        ],
    )
    llm = _FakeLLM(scripts=[[
        TextDelta(text="hi", index=0),
        Done(stop_reason="stop", raw_reason="stop"),
    ]])
    executor, _ = await _build(
        graph=graph, llm=llm, agents={"x": _agent("x")},
        turn_log_storage=None,
    )
    # Doesn't raise; trivially runs.
    await _drain(executor.invoke([]))
