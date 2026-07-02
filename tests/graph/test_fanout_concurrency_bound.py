"""Per-superstep fan-out concurrency bound (BE5).

A wide ``map`` fan-out spawns one node task per ready instance. Without an
admission bound every instance's agent loop (an LLM call + persistence) would
run concurrently. ``max_parallel_nodes`` caps how many node bodies execute at
once WITHOUT dropping any: every ready node still runs, just <=N at a time.

The test drives ``Begin -> planner -> FanOut(map) -> worker`` with 6 worker
instances and ``max_parallel_nodes=2``, gating the worker LLM on a barrier so
the concurrency counter can be observed. With the cap in place the peak of
simultaneously-active worker bodies never exceeds 2, yet all 6 still run.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, Generic, TypeVar

import pytest

from primer.graph.executor import GraphExecutor
from primer.graph.router import RouterRegistry
from primer.model.agent import Agent, AgentModel
from primer.model.chat import Done, Message, StreamEvent, TextDelta
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


class _ConcurrencyProbeLLM:
    """LLM that records how many worker turns run concurrently.

    The planner call (the first) returns the topic list immediately. Every
    subsequent (worker) call increments an ``active`` gauge, records the peak,
    then blocks on a shared barrier so siblings pile up — this is what makes a
    missing bound observable. The test releases the barrier once it has
    confirmed the peak plateaued at the cap.
    """

    def __init__(self, topics: list[str]) -> None:
        self._topics = topics
        self._first = True
        self.active = 0
        self.peak = 0
        self.entered = 0
        self.release = asyncio.Event()

    async def list_models(self):
        return ["m"]

    def stream(self, *, model: str, messages: list[Message], **kwargs: Any):
        first = self._first
        self._first = False
        if first:
            return self._planner_stream()
        return self._worker_stream()

    async def _planner_stream(self) -> AsyncIterator[StreamEvent]:
        import json

        yield TextDelta(text=json.dumps({"topics": self._topics}), index=0)
        yield Done(stop_reason="stop", raw_reason="stop")

    async def _worker_stream(self) -> AsyncIterator[StreamEvent]:
        self.active += 1
        self.entered += 1
        self.peak = max(self.peak, self.active)
        try:
            await self.release.wait()
        finally:
            self.active -= 1
        yield TextDelta(text="ack", index=0)
        yield Done(stop_reason="stop", raw_reason="stop")


def _agent(agent_id: str) -> Agent:
    return Agent(
        id=agent_id,
        description=f"agent {agent_id}",
        model=AgentModel(provider_id="p", model_name="m"),
        system_prompt=[],
    )


async def _build_executor(*, graph: Graph, llm: _ConcurrencyProbeLLM, cap: int):
    async def agent_resolver(agent_id: str) -> Agent:
        return _agent(agent_id)

    async def llm_resolver(agent: Agent):
        return (llm, LLMModel(name="m", context_length=128_000))

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
        max_parallel_nodes=cap,
    )
    return executor


def _wide_map_graph(n: int) -> Graph:
    return Graph.model_construct(
        id="g-bound",
        description="Begin -> planner -> FanOut(map) -> worker -> end",
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
                input_template="Topic: {{ fanout_item }}",
            ),
            _EndNode(id="end"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="planner"),
            _StaticEdge(from_node="planner", to_node="fan"),
            _StaticEdge(from_node="worker", to_node="end"),
        ],
        max_iterations=20,
        harness_id=None,
    )


@pytest.mark.asyncio
async def test_fanout_peak_concurrency_respects_cap() -> None:
    n = 6
    cap = 2
    topics = [f"t{i}" for i in range(n)]
    llm = _ConcurrencyProbeLLM(topics)
    graph = _wide_map_graph(n)
    executor = await _build_executor(graph=graph, llm=llm, cap=cap)

    events: list[Any] = []

    async def _drive() -> None:
        async for ev in executor.invoke([]):
            events.append(ev)

    drive = asyncio.create_task(_drive())

    # Wait until the worker superstep has admitted exactly `cap` workers and
    # they are all parked on the barrier. If the bound were missing, `entered`
    # would climb to n instead of plateauing at cap.
    async def _wait_plateau() -> None:
        deadline = asyncio.get_event_loop().time() + 5.0
        while asyncio.get_event_loop().time() < deadline:
            if llm.active >= cap:
                # Give any (erroneously) unbounded extra workers a chance to
                # enter before asserting the plateau.
                await asyncio.sleep(0.1)
                return
            await asyncio.sleep(0.01)
        raise AssertionError("workers never reached the cap")

    await _wait_plateau()

    # The critical assertion: only `cap` worker bodies ran while the barrier
    # was held — the rest are still blocked on the semaphore, not the barrier.
    assert llm.active == cap, f"active={llm.active} exceeded cap={cap}"
    assert llm.entered == cap, f"entered={llm.entered} — bound not applied"

    # Release the barrier; the remaining workers now drain in <=cap waves.
    llm.release.set()
    await asyncio.wait_for(drive, timeout=10.0)

    # Every worker ran (nothing dropped) and the peak never exceeded the cap.
    assert llm.entered == n
    assert llm.peak == cap, f"peak={llm.peak} exceeded cap={cap}"


@pytest.mark.asyncio
async def test_fanout_unbounded_when_cap_ge_width() -> None:
    """Sanity: with cap >= width every worker runs at once (peak == n).

    Confirms the bound is the ONLY thing limiting concurrency — remove it (by
    setting cap high) and all `n` workers run simultaneously.
    """
    n = 5
    topics = [f"t{i}" for i in range(n)]
    llm = _ConcurrencyProbeLLM(topics)
    graph = _wide_map_graph(n)
    executor = await _build_executor(graph=graph, llm=llm, cap=32)

    events: list[Any] = []

    async def _drive() -> None:
        async for ev in executor.invoke([]):
            events.append(ev)

    drive = asyncio.create_task(_drive())

    async def _wait_all_entered() -> None:
        deadline = asyncio.get_event_loop().time() + 5.0
        while asyncio.get_event_loop().time() < deadline:
            if llm.entered >= n:
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"only {llm.entered}/{n} workers entered")

    await _wait_all_entered()
    assert llm.active == n
    llm.release.set()
    await asyncio.wait_for(drive, timeout=10.0)
    assert llm.peak == n
