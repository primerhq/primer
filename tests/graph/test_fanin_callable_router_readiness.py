"""Regression: FanIn readiness must count callable-router incoming edges.

BUG 2: ``_edges_by_to`` (the per-target incoming-edge index that backs
``_fanin_ready``) only recorded static + json-path-conditional edges and
SKIPPED callable-router edges. A FanIn fed by a callable-router branch
could therefore fire BEFORE that branch completed.

Topology:

    begin --static--> a --static--> fanin
    begin --static--> b --callable(route_to_fanin)--> fanin

``a`` resolves in one hop; ``b`` routes into ``fanin`` via a callable
router, and that hop lands one superstep later than ``a``'s static edge.
With the bug, the FanIn fires as soon as ``a`` completes (its only
statically-known upstream), so ``nodes.c``... here ``nodes.b`` is missing
from the aggregate. The fix makes the FanIn wait for both upstreams.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Generic, TypeVar

import pytest

from primer.graph.base import _GraphEndOutputEvent
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
    Graph,
    GraphNodeMessage,
    GraphThread,
    _AgentNodeRef,
    _BeginNode,
    _CallableRouter,
    _ConditionalEdge,
    _EndNode,
    _FanInNode,
    _StaticEdge,
)
from primer.model.provider import LLMModel
from primer.model.storage import (
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


class _EchoLLM:
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
        return self._stream(text)

    async def _stream(self, text: str) -> AsyncIterator[StreamEvent]:
        yield TextDelta(text=text or "ok", index=0)
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


async def _build_executor(*, graph: Graph, llm: _EchoLLM, registry: RouterRegistry):
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
        router_registry=registry,
    )
    return executor, thread, thread_storage


async def _drain(it):
    return [ev async for ev in it]


def _graph() -> Graph:
    return Graph.model_construct(
        id="g-callable-fanin",
        description="callable router into a FanIn",
        nodes=[
            _BeginNode(id="begin"),
            _AgentNodeRef(id="a", agent_id="ag", input_template="A"),
            _AgentNodeRef(id="b", agent_id="ag", input_template="B"),
            _AgentNodeRef(id="c", agent_id="ag", input_template="C"),
            _FanInNode(
                id="fanin",
                aggregate_template="{{ nodes.a.text }}|{{ nodes.c.text }}",
            ),
            _EndNode(id="end", output_template="{{ nodes.fanin.text }}"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="a"),
            _StaticEdge(from_node="begin", to_node="b"),
            _StaticEdge(from_node="a", to_node="fanin"),
            # b routes to c via a callable router, then c -> fanin via
            # another callable router. The c -> fanin hop is the one that
            # _edges_by_to used to drop.
            _ConditionalEdge(
                from_node="b",
                router=_CallableRouter(callable_id="b_to_c"),
            ),
            _ConditionalEdge(
                from_node="c",
                router=_CallableRouter(callable_id="c_to_fanin"),
            ),
            _StaticEdge(from_node="fanin", to_node="end"),
        ],
        max_iterations=10,
        harness_id=None,
    )


@pytest.mark.asyncio
async def test_fanin_waits_for_callable_router_upstream() -> None:
    registry = RouterRegistry()
    registry.register("b_to_c", lambda ctx, src: "c")
    registry.register("c_to_fanin", lambda ctx, src: "fanin")

    graph = _graph()
    llm = _EchoLLM()
    executor, thread, thread_storage = await _build_executor(
        graph=graph, llm=llm, registry=registry
    )

    # Capture the worker aggregate visible the moment fanin enters next_ready:
    # both `a` and `c` must have produced output before the FanIn admits.
    fanin_admit_snapshots: list[set[str]] = []
    original_compute = executor._compute_next_ready

    async def tap_compute(just_ran, context):
        result = await original_compute(just_ran, context)
        if "fanin" in result:
            fanin_admit_snapshots.append(set(context.nodes.keys()))
        return result

    executor._compute_next_ready = tap_compute  # type: ignore[method-assign]

    events = await _drain(executor.invoke([]))

    final = await thread_storage.get(thread.id)
    assert final is not None
    assert final.ended_reason == "completed", (
        f"expected completed; got {final.ended_reason!r} / {final.ended_detail!r}"
    )

    # The FanIn must not be admitted until BOTH upstreams (`a` via static and
    # `c` via callable router) have produced output.
    assert fanin_admit_snapshots, "FanIn never entered the ready set"
    for snap in fanin_admit_snapshots:
        assert "c" in snap, (
            "FanIn admitted before its callable-router upstream `c` completed; "
            f"context had: {sorted(snap)}"
        )

    end_events = [ev for ev in events if isinstance(ev, _GraphEndOutputEvent)]
    assert end_events, "expected an End output event"
    assert end_events[-1].text == "A|C", (
        f"unexpected aggregate: {end_events[-1].text!r}"
    )


def _graph_router_away() -> Graph:
    """A callable-router source (`b`) that routes AWAY from the FanIn.

    begin --static--> a   --static----------> fanin --static--> end
    begin --static--> b   --callable(end2)--> end2

    ``b`` owns a callable-router out-edge, so it is a ``_callable_router_source``
    and gets admitted. The FanIn's gate must wait for ``b`` to produce output
    (its routing decision is unknown until then), but once ``b`` resolves and
    routes to its own ``end2`` it stops blocking. The FanIn must still fire from
    ``a`` and the run must COMPLETE -- a live-but-routing-away source must not
    dead-lock the FanIn. (``b`` routes to a second, independent End so it never
    touches the FanIn branch.)
    """
    return Graph.model_construct(
        id="g-router-away",
        description="callable router that routes away from a FanIn",
        nodes=[
            _BeginNode(id="begin"),
            _AgentNodeRef(id="a", agent_id="ag", input_template="A"),
            _AgentNodeRef(id="b", agent_id="ag", input_template="B"),
            _FanInNode(id="fanin", aggregate_template="{{ nodes.a.text }}"),
            _EndNode(id="end", output_template="{{ nodes.fanin.text }}"),
            _EndNode(id="end2", output_template="{{ nodes.b.text }}"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="a"),
            _StaticEdge(from_node="begin", to_node="b"),
            _StaticEdge(from_node="a", to_node="fanin"),
            _ConditionalEdge(
                from_node="b",
                router=_CallableRouter(callable_id="b_to_end"),
            ),
            _StaticEdge(from_node="fanin", to_node="end"),
        ],
        max_iterations=10,
        harness_id=None,
    )


@pytest.mark.asyncio
async def test_fanin_not_deadlocked_by_router_away_source() -> None:
    """A live callable-router source that routes away must not block forever."""
    registry = RouterRegistry()
    registry.register("b_to_end", lambda ctx, src: "end2")

    graph = _graph_router_away()
    llm = _EchoLLM()
    executor, thread, thread_storage = await _build_executor(
        graph=graph, llm=llm, registry=registry
    )

    events = await _drain(executor.invoke([]))

    final = await thread_storage.get(thread.id)
    assert final is not None
    assert final.ended_reason == "completed", (
        f"router-away source dead-locked the FanIn; "
        f"got {final.ended_reason!r} / {final.ended_detail!r}"
    )
    end_events = [ev for ev in events if isinstance(ev, _GraphEndOutputEvent)]
    assert end_events, "expected an End output event"
    end_texts = {ev.text for ev in end_events}
    assert "A" in end_texts, (
        f"FanIn -> End never fired; end texts were: {sorted(end_texts)!r}"
    )


def _graph_router_never_activates() -> Graph:
    """A callable-router source (`z`) that is NEVER reached on this run.

    begin --callable(a)--> a --static--> fanin
                           z --callable(fanin)--> fanin   (z never admitted)
              fanin --static--> end

    ``begin``'s callable router only ever routes to ``a``; ``z`` is a
    ``_callable_router_source`` but is never scheduled, so it is never
    ``_admitted`` and must NOT gate the FanIn. The run must COMPLETE.
    """
    return Graph.model_construct(
        id="g-router-never",
        description="callable-router source that never activates",
        nodes=[
            _BeginNode(id="begin"),
            _AgentNodeRef(id="a", agent_id="ag", input_template="A"),
            _AgentNodeRef(id="z", agent_id="ag", input_template="Z"),
            _FanInNode(id="fanin", aggregate_template="{{ nodes.a.text }}"),
            _EndNode(id="end", output_template="{{ nodes.fanin.text }}"),
        ],
        edges=[
            _ConditionalEdge(
                from_node="begin",
                router=_CallableRouter(callable_id="begin_to_a"),
            ),
            _StaticEdge(from_node="a", to_node="fanin"),
            _ConditionalEdge(
                from_node="z",
                router=_CallableRouter(callable_id="z_to_fanin"),
            ),
            _StaticEdge(from_node="fanin", to_node="end"),
        ],
        max_iterations=10,
        harness_id=None,
    )


@pytest.mark.asyncio
async def test_fanin_not_deadlocked_by_inactive_router_source() -> None:
    """A callable-router source that never activates must not gate the FanIn."""
    registry = RouterRegistry()
    registry.register("begin_to_a", lambda ctx, src: "a")
    registry.register("z_to_fanin", lambda ctx, src: "fanin")

    graph = _graph_router_never_activates()
    llm = _EchoLLM()
    executor, thread, thread_storage = await _build_executor(
        graph=graph, llm=llm, registry=registry
    )

    events = await _drain(executor.invoke([]))

    final = await thread_storage.get(thread.id)
    assert final is not None
    assert final.ended_reason == "completed", (
        f"inactive callable-router source dead-locked the FanIn; "
        f"got {final.ended_reason!r} / {final.ended_detail!r}"
    )
    end_events = [ev for ev in events if isinstance(ev, _GraphEndOutputEvent)]
    assert end_events, "expected an End output event"
    assert end_events[-1].text == "A", (
        f"unexpected aggregate: {end_events[-1].text!r}"
    )
