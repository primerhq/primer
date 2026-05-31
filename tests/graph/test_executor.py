"""Tests for primer.graph.executor.GraphExecutor."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any, Generic, TypeVar

import pytest

from primer.graph.executor import GraphExecutor
from primer.graph.router import RouterRegistry
from primer.model.agent import Agent, AgentModel
from primer.model.chat import (
    Done,
    ExtendedEvent,
    Message,
    StreamEvent,
    TextDelta,
    TextPart,
    _GraphNodeEvent,
)
from primer.model.common import Identifiable
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.graph import (
    Graph,
    GraphContext,
    GraphNodeMessage,
    GraphThread,
    JsonPathBranch,
    NodeOutput,
    _AgentNodeRef,
    _BeginNode,
    _CallableRouter,
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


# ===========================================================================
# In-memory storage double
# ===========================================================================


_T = TypeVar("_T", bound=Identifiable)


class _InMemoryStorage(Generic[_T]):
    """Bare-minimum :class:`Storage` test double."""

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


# ===========================================================================
# FakeLLM
# ===========================================================================


class _FakeLLM:
    """Stub :class:`LLM` returning scripted streams keyed by call count."""

    def __init__(self, *, scripts: list[list[StreamEvent]]) -> None:
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

    async def _stream_impl(
        self,
        events: list[StreamEvent],
    ) -> AsyncIterator[StreamEvent]:
        for ev in events:
            yield ev


# ===========================================================================
# Helpers
# ===========================================================================


def _agent(agent_id: str, *, system_prompt: list[str] | None = None) -> Agent:
    return Agent(
        id=agent_id,
        description=f"agent {agent_id}",
        model=AgentModel(provider_id="p", model_name="m"),
        system_prompt=list(system_prompt or []),
    )


def _model() -> LLMModel:
    return LLMModel(name="m", context_length=128_000)


async def _build_executor(
    *,
    graph: Graph,
    llm: _FakeLLM,
    agents: dict[str, Agent] | None = None,
    router_registry: RouterRegistry | None = None,
):
    if agents is None:
        agents = {}

    async def agent_resolver(agent_id: str) -> Agent:
        if agent_id not in agents:
            raise KeyError(agent_id)
        return agents[agent_id]

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
        router_registry=router_registry,
    )
    return executor, thread, thread_storage, message_storage


async def _drain(it: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    return [ev async for ev in it]


# ===========================================================================
# Linear graph A -> terminal
# ===========================================================================


class TestLinearGraph:
    @pytest.mark.asyncio
    async def test_single_node_to_terminal(self) -> None:
        graph = Graph(
            id="g-linear",
            description="A -> exit",
            entry_node_id="begin",
            nodes=[
                _BeginNode(id="begin"),
                _AgentNodeRef(id="A", agent_id="agent-x"),
                _EndNode(id="exit"),
            ],
            edges=[
                _StaticEdge(from_node="begin", to_node="A"),
                _StaticEdge(from_node="A", to_node="exit"),
            ],
        )
        llm = _FakeLLM(
            scripts=[
                [
                    TextDelta(text="hello", index=0),
                    Done(stop_reason="stop", raw_reason="stop"),
                ]
            ]
        )
        executor, thread, ts, ms = await _build_executor(
            graph=graph,
            llm=llm,
            agents={"agent-x": _agent("agent-x")},
        )
        events = await _drain(executor.invoke([]))
        wrapped = [
            e for e in events
            if isinstance(e, ExtendedEvent)
            and isinstance(e.extended, _GraphNodeEvent)
        ]
        assert len(wrapped) == 2
        assert all(w.extended.node_id == "A" for w in wrapped)  # type: ignore[union-attr]
        loaded = await ts.get(thread.id)
        assert loaded is not None
        assert loaded.status == SessionStatus.ENDED
        assert loaded.ended_reason == "completed"
        rows = sorted(ms._data.values(), key=lambda r: r.sequence)
        a_rows = [r for r in rows if r.node_id == "A"]
        assert len(a_rows) == 2
        assert a_rows[0].role == "user"
        assert a_rows[1].role == "assistant"


# ===========================================================================
# Fan-out + fan-in: A -> (B, C) -> D -> exit
# ===========================================================================


class TestFanOutFanIn:
    @pytest.mark.asyncio
    async def test_fan_out_fan_in(self) -> None:
        graph = Graph(
            id="g-fan",
            description="A -> (B, C) -> D -> exit",
            entry_node_id="begin",
            nodes=[
                _BeginNode(id="begin"),
                _AgentNodeRef(id="A", agent_id="x"),
                _AgentNodeRef(
                    id="B",
                    agent_id="x",
                    input_template="from-A: {{ nodes.A.text }}",
                ),
                _AgentNodeRef(
                    id="C",
                    agent_id="x",
                    input_template="from-A: {{ nodes.A.text }}",
                ),
                _AgentNodeRef(
                    id="D",
                    agent_id="x",
                    input_template=(
                        "B: {{ nodes.B.text }} | C: {{ nodes.C.text }}"
                    ),
                ),
                _EndNode(id="exit"),
            ],
            edges=[
                _StaticEdge(from_node="begin", to_node="A"),
                _StaticEdge(from_node="A", to_node="B"),
                _StaticEdge(from_node="A", to_node="C"),
                _StaticEdge(from_node="B", to_node="D"),
                _StaticEdge(from_node="C", to_node="D"),
                _StaticEdge(from_node="D", to_node="exit"),
            ],
        )
        scripts = [
            [
                TextDelta(text=f"reply-{i}", index=0),
                Done(stop_reason="stop", raw_reason="stop"),
            ]
            for i in range(4)
        ]
        llm = _FakeLLM(scripts=scripts)
        executor, _, _, _ = await _build_executor(
            graph=graph,
            llm=llm,
            agents={"x": _agent("x")},
        )
        await _drain(executor.invoke([]))
        assert len(llm.calls) == 4
        d_call = llm.calls[-1]
        last_user = d_call["messages"][-1]
        assert last_user.role == "user"
        text = last_user.parts[0].text
        assert "B:" in text and "C:" in text


# ===========================================================================
# Cycle with max_iterations
# ===========================================================================


class TestCycle:
    @pytest.mark.asyncio
    async def test_max_iterations_terminates(self) -> None:
        graph = Graph(
            id="g-loop",
            description="A -> A forever (bounded)",
            entry_node_id="begin",
            # +1 vs. the legacy fixture because the Begin step counts as
            # iteration 0 in the executor's superstep loop.
            max_iterations=4,
            nodes=[
                _BeginNode(id="begin"),
                _AgentNodeRef(
                    id="A",
                    agent_id="x",
                    response_format={"type": "object"},
                ),
                # Reachability declaration only — the loop below always
                # routes back to A so this End never actually fires.
                _EndNode(id="exit"),
            ],
            edges=[
                _StaticEdge(from_node="begin", to_node="A"),
                _ConditionalEdge(
                    from_node="A",
                    router=_JsonPathRouter(
                        branches=[
                            JsonPathBranch(conditions=[], to_node="A"),
                        ],
                        default_to="exit",
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
        executor, thread, ts, _ = await _build_executor(
            graph=graph, llm=llm, agents={"x": _agent("x")}
        )
        await _drain(executor.invoke([]))
        loaded = await ts.get(thread.id)
        assert loaded is not None
        assert loaded.status == SessionStatus.ENDED
        # Spec A §5.4: ended_reason is the bucket; the detail code carries
        # the bound-exceeded signal so the public contract has a finite
        # `ended_reason` enum and an open-ended `ended_detail` string.
        assert loaded.ended_reason == "failed"
        assert loaded.ended_detail == "max_iterations_exceeded"
        assert len(llm.calls) == 3


# ===========================================================================
# Conditional routing
# ===========================================================================


class TestConditionalRouting:
    @pytest.mark.asyncio
    async def test_jsonpath_router(self) -> None:
        graph = Graph(
            id="g-cond",
            description="A -> route on parsed",
            entry_node_id="begin",
            max_iterations=5,
            nodes=[
                _BeginNode(id="begin"),
                _AgentNodeRef(
                    id="A",
                    agent_id="x",
                    response_format={"type": "object"},
                ),
                _AgentNodeRef(id="B", agent_id="x"),
                _EndNode(id="exit"),
            ],
            edges=[
                _StaticEdge(from_node="begin", to_node="A"),
                _ConditionalEdge(
                    from_node="A",
                    router=_JsonPathRouter(
                        branches=[
                            JsonPathBranch(when={"go": "exit"}, to_node="exit"),
                            JsonPathBranch(when={"go": "B"}, to_node="B"),
                        ],
                    ),
                ),
                _StaticEdge(from_node="B", to_node="exit"),
            ],
        )
        llm = _FakeLLM(
            scripts=[
                [
                    TextDelta(text='{"go": "exit"}', index=0),
                    Done(stop_reason="stop", raw_reason="stop"),
                ]
            ]
        )
        executor, thread, ts, _ = await _build_executor(
            graph=graph, llm=llm, agents={"x": _agent("x")}
        )
        await _drain(executor.invoke([]))
        loaded = await ts.get(thread.id)
        assert loaded is not None
        assert loaded.ended_reason == "completed"
        assert len(llm.calls) == 1

    @pytest.mark.asyncio
    async def test_callable_router(self) -> None:
        graph = Graph(
            id="g-call",
            description="A -> route via callable",
            entry_node_id="begin",
            max_iterations=5,
            nodes=[
                _BeginNode(id="begin"),
                _AgentNodeRef(id="A", agent_id="x"),
                _AgentNodeRef(id="B", agent_id="x"),
                _EndNode(id="exit"),
            ],
            edges=[
                _StaticEdge(from_node="begin", to_node="A"),
                _ConditionalEdge(
                    from_node="A",
                    router=_CallableRouter(callable_id="my-router"),
                ),
                _StaticEdge(from_node="B", to_node="exit"),
            ],
        )

        def my_router(ctx: GraphContext, source: NodeOutput) -> str:
            return "exit"

        reg = RouterRegistry()
        reg.register("my-router", my_router)

        llm = _FakeLLM(
            scripts=[
                [
                    TextDelta(text="ok", index=0),
                    Done(stop_reason="stop", raw_reason="stop"),
                ]
            ]
        )
        executor, thread, ts, _ = await _build_executor(
            graph=graph,
            llm=llm,
            agents={"x": _agent("x")},
            router_registry=reg,
        )
        await _drain(executor.invoke([]))
        loaded = await ts.get(thread.id)
        assert loaded is not None
        assert loaded.ended_reason == "completed"
        assert len(llm.calls) == 1


# ===========================================================================
# Failure handling
# ===========================================================================


class TestFailures:
    @pytest.mark.asyncio
    async def test_jsonpath_router_no_match_no_default_fails_graph(self) -> None:
        graph = Graph(
            id="g-fail",
            description="A -> no-match",
            entry_node_id="begin",
            max_iterations=5,
            nodes=[
                _BeginNode(id="begin"),
                _AgentNodeRef(
                    id="A",
                    agent_id="x",
                    response_format={"type": "object"},
                ),
                _AgentNodeRef(id="B", agent_id="x"),
                _EndNode(id="exit"),
            ],
            edges=[
                _StaticEdge(from_node="begin", to_node="A"),
                _ConditionalEdge(
                    from_node="A",
                    router=_JsonPathRouter(
                        branches=[JsonPathBranch(when={"go": "B"}, to_node="B")],
                    ),
                ),
                _StaticEdge(from_node="B", to_node="exit"),
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
        executor, thread, ts, _ = await _build_executor(
            graph=graph, llm=llm, agents={"x": _agent("x")}
        )
        await _drain(executor.invoke([]))
        loaded = await ts.get(thread.id)
        assert loaded is not None
        assert loaded.ended_reason == "failed"


# ===========================================================================
# Thread management
# ===========================================================================


class TestThreadManagement:
    @pytest.mark.asyncio
    async def test_open_and_delete_thread(self) -> None:
        graph = Graph(
            id="g-trivial",
            description="trivial",
            entry_node_id="begin",
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
        ts: _InMemoryStorage[GraphThread] = _InMemoryStorage(GraphThread)
        ms: _InMemoryStorage[GraphNodeMessage] = _InMemoryStorage(GraphNodeMessage)
        thread = await GraphExecutor.open_thread(
            graph=graph, thread_storage=ts, title="t"  # type: ignore[arg-type]
        )
        await ms.create(
            GraphNodeMessage(
                id="gnm-1",
                graph_thread_id=thread.id,
                node_id="A",
                role="user",
                parts=[TextPart(text="hi")],
                created_at=datetime.now(timezone.utc),
                iteration=0,
                sequence=0,
            )
        )
        await GraphExecutor.delete_thread(
            thread.id,
            thread_storage=ts,  # type: ignore[arg-type]
            message_storage=ms,  # type: ignore[arg-type]
        )
        assert await ts.get(thread.id) is None
        assert await ms.get("gnm-1") is None

    @pytest.mark.asyncio
    async def test_list_threads_filter_by_graph_id(self) -> None:
        ts: _InMemoryStorage[GraphThread] = _InMemoryStorage(GraphThread)
        graph_a = Graph(
            id="g-a",
            description="x",
            entry_node_id="begin",
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
        graph_b = Graph(
            id="g-b",
            description="y",
            entry_node_id="begin",
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
        await GraphExecutor.open_thread(
            graph=graph_a, thread_storage=ts  # type: ignore[arg-type]
        )
        await GraphExecutor.open_thread(
            graph=graph_a, thread_storage=ts  # type: ignore[arg-type]
        )
        await GraphExecutor.open_thread(
            graph=graph_b, thread_storage=ts  # type: ignore[arg-type]
        )
        page = await GraphExecutor.list_threads(
            thread_storage=ts,  # type: ignore[arg-type]
            page=OffsetPage(length=10),
            graph_id="g-a",
        )
        assert page.length == 2  # type: ignore[union-attr]
