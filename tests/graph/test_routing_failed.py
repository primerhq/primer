"""Conditional edge with no matching branch and no default_to ends the
graph with ``ended_detail='routing_failed'`` and a :class:`_GraphErrorEvent`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Generic, TypeVar

import pytest

from primer.graph.base import _GraphErrorEvent
from primer.graph.executor import GraphExecutor
from primer.graph.router import RouterRegistry
from primer.model.agent import Agent, AgentModel
from primer.model.chat import Done, Message, StreamEvent, TextDelta, TextPart
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
    _EndNode,
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


async def _build_executor(*, graph: Graph, llm: _FakeLLM, agents: dict[str, Agent]):
    async def agent_resolver(agent_id: str):
        return agents[agent_id]

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
    return executor, thread, thread_storage


async def _drain(it):
    return [ev async for ev in it]


@pytest.mark.asyncio
async def test_no_match_no_default_emits_routing_failed() -> None:
    """Agent returns a parsed dict that matches no branch and there's no
    default_to; expect ended_reason=failed, ended_detail=routing_failed,
    and a _GraphErrorEvent on the stream."""
    graph = Graph(
        id="g-routing-fail",
        description="Begin -> A -> conditional (no match, no default) -> end",
        max_iterations=5,
        nodes=[
            _BeginNode(id="b"),
            _AgentNodeRef(
                id="a",
                agent_id="x",
                response_format={"type": "object"},
            ),
            _EndNode(id="end"),
        ],
        edges=[
            _StaticEdge(from_node="b", to_node="a"),
            _ConditionalEdge(
                from_node="a",
                router=_JsonPathRouter(
                    branches=[
                        JsonPathBranch(
                            conditions=[
                                BranchCondition(path="go", op="eq", value="end")
                            ],
                            to_node="end",
                        )
                    ],
                ),
            ),
        ],
    )
    llm = _FakeLLM(
        scripts=[
            [
                TextDelta(text='{"go": "nope"}', index=0),
                Done(stop_reason="stop", raw_reason="stop"),
            ]
        ]
    )
    executor, thread, ts = await _build_executor(
        graph=graph, llm=llm, agents={"x": _agent("x")}
    )
    events = await _drain(executor.invoke([]))
    err_events = [ev for ev in events if isinstance(ev, _GraphErrorEvent)]
    assert err_events, "expected at least one _GraphErrorEvent"
    assert err_events[0].code == "routing_failed"
    assert err_events[0].node_id == "a"
    loaded = await ts.get(thread.id)
    assert loaded is not None
    assert loaded.status == SessionStatus.ENDED
    assert loaded.ended_reason == "failed"
    assert loaded.ended_detail == "routing_failed"


# Fenced JSON: the agent-node parse (`json.loads`) fails on a ```json
# code fence, so NodeOutput.parsed is None. A json_path edge must treat
# this as "no branch matched" -- route to default_to when set, else a
# CODED routing_failed -- never an uncoded ConfigError the executor
# swallows into a detail-less failure.
_FENCED_JSON = '```json\n{"go": "end"}\n```'


@pytest.mark.asyncio
async def test_null_parsed_with_default_to_routes_to_default() -> None:
    """A json_path source whose structured output doesn't parse (parsed is
    None) routes to ``default_to`` instead of crashing the graph."""
    graph = Graph(
        id="g-null-parsed-default",
        description="Begin -> A -> conditional (null parsed, default_to=end) -> end",
        max_iterations=5,
        nodes=[
            _BeginNode(id="b"),
            _AgentNodeRef(id="a", agent_id="x", response_format={"type": "object"}),
            _EndNode(id="end"),
        ],
        edges=[
            _StaticEdge(from_node="b", to_node="a"),
            _ConditionalEdge(
                from_node="a",
                router=_JsonPathRouter(
                    branches=[
                        JsonPathBranch(
                            conditions=[
                                BranchCondition(path="go", op="eq", value="loop")
                            ],
                            to_node="a",
                        )
                    ],
                    default_to="end",
                ),
            ),
        ],
    )
    llm = _FakeLLM(
        scripts=[
            [
                TextDelta(text=_FENCED_JSON, index=0),
                Done(stop_reason="stop", raw_reason="stop"),
            ]
        ]
    )
    executor, thread, ts = await _build_executor(
        graph=graph, llm=llm, agents={"x": _agent("x")}
    )
    events = await _drain(executor.invoke([]))
    assert not [ev for ev in events if isinstance(ev, _GraphErrorEvent)]
    loaded = await ts.get(thread.id)
    assert loaded is not None
    assert loaded.status == SessionStatus.ENDED
    assert loaded.ended_reason == "completed"
    assert loaded.ended_detail is None


@pytest.mark.asyncio
async def test_null_parsed_without_default_to_emits_routing_failed() -> None:
    """A json_path source whose structured output doesn't parse and that has
    no ``default_to`` ends with a CODED ``routing_failed`` (not an uncoded
    ConfigError swallowed into ended_detail=None)."""
    graph = Graph(
        id="g-null-parsed-nofallback",
        description="Begin -> A -> conditional (null parsed, no default) -> end",
        max_iterations=5,
        nodes=[
            _BeginNode(id="b"),
            _AgentNodeRef(id="a", agent_id="x", response_format={"type": "object"}),
            _EndNode(id="end"),
        ],
        edges=[
            _StaticEdge(from_node="b", to_node="a"),
            _ConditionalEdge(
                from_node="a",
                router=_JsonPathRouter(
                    branches=[
                        JsonPathBranch(
                            conditions=[
                                BranchCondition(path="go", op="eq", value="end")
                            ],
                            to_node="end",
                        )
                    ],
                ),
            ),
        ],
    )
    llm = _FakeLLM(
        scripts=[
            [
                TextDelta(text=_FENCED_JSON, index=0),
                Done(stop_reason="stop", raw_reason="stop"),
            ]
        ]
    )
    executor, thread, ts = await _build_executor(
        graph=graph, llm=llm, agents={"x": _agent("x")}
    )
    events = await _drain(executor.invoke([]))
    err = [ev for ev in events if isinstance(ev, _GraphErrorEvent)]
    assert err and err[0].code == "routing_failed"
    assert err[0].node_id == "a"
    loaded = await ts.get(thread.id)
    assert loaded is not None
    assert loaded.ended_reason == "failed"
    assert loaded.ended_detail == "routing_failed"
