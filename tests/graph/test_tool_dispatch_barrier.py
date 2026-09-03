"""_ToolDispatchBarrier: the graph surface's FIFO drain-through guarantee.

01a0518b seam-split A-graph surface. Unlike chat/workspace (a pull-based
async-generator chain where the consumer's translate_stream_event+append
runs synchronously before the producer's next yield can be requested), the
graph surface pushes events onto an UNBOUNDED asyncio.Queue: ``queue.put``
never suspends, so a node's own task can race far ahead of the drainer
with nothing to make it wait. The (future) seam-split dispatch needs to
know a batch's ToolCallStart/ToolCallEnd events have actually been
translated+appended by dispatch.py before it reads their seqs back out of
coalesce state — this is the barrier that provides that guarantee.

This test drives a REAL two-worker fan-out (Begin -> planner -> FanOut(map)
-> worker -> End) through the real GraphExecutor/`_run_superstep_loop`
machinery. One worker emits a real ToolCallStart onto the real shared
per-superstep queue and immediately (racing) posts+awaits a
`_ToolDispatchBarrier` on that same queue via `await_tool_dispatch_barrier`;
the other worker runs a plain text turn concurrently, so the barrier's
ordering guarantee is proven under genuine interleaved multi-node queue
traffic, not a synthetic single-producer queue. The test's own consumption
of the ToolCallStart event (standing in for dispatch.py's
translate_stream_event + append) is deliberately delayed, so the barrier
resolving only AFTER that delay completes is proof the mechanism enforces
the ordering — not a timing coincidence.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any, Generic, TypeVar
from unittest import mock

import pytest

from primer.graph._node_refs import await_tool_dispatch_barrier
from primer.graph.executor import GraphExecutor
from primer.graph.router import RouterRegistry
from primer.model.chat import (
    Done,
    ExtendedEvent,
    Message,
    StreamEvent,
    TextDelta,
    ToolCallEnd,
    ToolCallStart,
    _GraphNodeEvent,
)
from primer.model.agent import Agent, AgentModel
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
from primer.model_profile import ResolvedModel
from primer.model.model_profile import ModelProfileConfig


_T = TypeVar("_T", bound=Identifiable)


# ---------------------------------------------------------------------------
# _InMemoryStorage — mirrors tests/graph/test_fanout_concurrency_bound.py
# ---------------------------------------------------------------------------


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

    async def update_unless(self, entity, *, field, forbidden, conn=None):
        current = self._data.get(entity.id)
        if current is None:
            raise NotFoundError(f"no entity with id {entity.id!r}")
        if getattr(current, field, None) == forbidden:
            return None
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
                offset=page.offset, length=len(sliced), total=len(items), items=sliced,
            )
        offset = int(page.cursor) if page.cursor else 0
        sliced = items[offset : offset + page.length]
        next_cursor = str(offset + page.length) if offset + page.length < len(items) else None
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


# ---------------------------------------------------------------------------
# Fake LLM — planner returns two topics; "race" worker emits a real tool
# call, "plain" worker just answers with text (concurrent queue noise).
# ---------------------------------------------------------------------------


class _RacingProbeLLM:
    def __init__(self) -> None:
        self._first = True
        self._race_rounds = 0

    async def list_models(self):
        return ["m"]

    def stream(self, *, model: str, messages: list[Message], **kwargs: Any):
        if self._first:
            self._first = False
            return self._planner_stream()
        # Find the topic from the last TextPart in the prompt. A second
        # tool-round appends a tool-role message whose parts are
        # ToolResultParts (no .text), so plain messages[-1] breaks on
        # round 2 - search backward for the original "Topic: race" /
        # "Topic: plain" user message instead.
        topic: str | None = None
        for msg in reversed(messages):
            for part in msg.parts:
                if getattr(part, "type", None) == "text":
                    topic = part.text
                    break
            if topic is not None:
                break
        if topic and "race" in topic:
            return self._race_worker_stream()
        return self._plain_worker_stream()

    async def _planner_stream(self) -> AsyncIterator[StreamEvent]:
        import json
        yield TextDelta(text=json.dumps({"topics": ["race", "plain"]}), index=0)
        yield Done(stop_reason="stop", raw_reason="stop")

    async def _race_worker_stream(self) -> AsyncIterator[StreamEvent]:
        self._race_rounds += 1
        if self._race_rounds > 1:
            # Second round (post tool-error): end the turn cleanly.
            yield TextDelta(text="ack", index=0)
            yield Done(stop_reason="stop", raw_reason="stop")
            return
        yield ToolCallStart(id="call_0", name="my_tool", index=0)
        yield ToolCallEnd(id="call_0", arguments={}, index=0)
        yield Done(stop_reason="stop", raw_reason="stop")

    async def _plain_worker_stream(self) -> AsyncIterator[StreamEvent]:
        yield TextDelta(text="ack", index=0)
        yield Done(stop_reason="stop", raw_reason="stop")


def _agent(agent_id: str) -> Agent:
    return Agent(
        id=agent_id,
        description=f"agent {agent_id}",
        model=AgentModel(profile_id="p--m"),
        system_prompt=[],
    )


async def _build_executor(*, graph: Graph, llm: _RacingProbeLLM):
    async def agent_resolver(agent_id: str) -> Agent:
        return _agent(agent_id)

    async def llm_resolver(agent: Agent):
        return (
            llm,
            ResolvedModel(
                profile_id="test-profile",
                provider_id="test-provider",
                model_name="m",
                context_length=128_000,
                config=ModelProfileConfig(),
            ),
        )

    thread_storage: _InMemoryStorage[GraphThread] = _InMemoryStorage(GraphThread)
    message_storage: _InMemoryStorage[GraphNodeMessage] = _InMemoryStorage(GraphNodeMessage)
    thread = await GraphExecutor.open_thread(
        graph=graph, thread_storage=thread_storage, title="t",  # type: ignore[arg-type]
    )
    executor = GraphExecutor(
        graph=graph,
        agent_resolver=agent_resolver,
        llm_resolver=llm_resolver,  # type: ignore[arg-type]
        thread_storage=thread_storage,  # type: ignore[arg-type]
        message_storage=message_storage,  # type: ignore[arg-type]
        graph_thread_id=thread.id,
        router_registry=RouterRegistry(),
        max_parallel_nodes=8,
    )
    return executor


def _two_worker_fanout_graph() -> Graph:
    return Graph.model_construct(
        id="g-barrier",
        description="Begin -> planner -> FanOut(map) -> worker -> end",
        nodes=[
            _BeginNode(id="begin"),
            _AgentNodeRef(
                id="planner",
                agent_id="ag-planner",
                response_format={
                    "type": "object",
                    "required": ["topics"],
                    "properties": {"topics": {"type": "array", "items": {"type": "string"}}},
                },
            ),
            _FanOutNode(
                id="fan",
                specs=[
                    FanOutSpec(
                        kind="map", target_node_id="worker",
                        source_node_id="planner", source_path="topics",
                    ),
                ],
            ),
            _AgentNodeRef(
                id="worker", agent_id="ag-worker",
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


def _is_race_tool_call_start(item: Any) -> bool:
    return (
        isinstance(item, ExtendedEvent)
        and isinstance(item.extended, _GraphNodeEvent)
        and item.extended.inner_type == "tool_call_start"
    )


@pytest.mark.asyncio
async def test_barrier_waits_for_prior_event_to_drain_under_fanout() -> None:
    """The barrier races a just-enqueued ToolCallStart and loses the race.

    The racing worker's own task posts a real ToolCallStart onto the
    shared queue, then IMMEDIATELY (in the same producer task, before the
    drainer necessarily gets a chance to run) posts+awaits the barrier via
    the same helper the seam split will use. The test's consumption of
    that ToolCallStart (standing in for dispatch.py's translate+append) is
    deliberately delayed 50ms. If the barrier resolved on enqueue/dequeue
    alone rather than on drain-through, it would resolve before that delay
    elapses; asserting it resolves after proves the ordering guarantee
    holds under real concurrent multi-node (fan-out) queue traffic, not
    just a single-producer toy queue.
    """
    llm = _RacingProbeLLM()
    graph = _two_worker_fanout_graph()
    executor = await _build_executor(graph=graph, llm=llm)

    consumed_tool_call_start_at: float | None = None
    barrier_resolved_at: float | None = None

    real_queue_holder: dict[str, "asyncio.Queue[Any]"] = {}

    class _PutSpyQueue:
        """Wraps the real per-superstep queue to hook ToolCallStart puts."""

        def __init__(self, inner: "asyncio.Queue[Any]") -> None:
            self._inner = inner
            real_queue_holder.setdefault("queue", inner)

        async def put(self, item: Any) -> None:
            await self._inner.put(item)
            if _is_race_tool_call_start(item):
                # Race: post + await the barrier immediately after the
                # real event landed in the queue, from the SAME producer
                # task, exactly like the eventual seam-split dispatch will.
                nonlocal barrier_resolved_at
                await await_tool_dispatch_barrier(self._inner)
                barrier_resolved_at = time.monotonic()

        async def get(self) -> Any:
            return await self._inner.get()

    original_stream_node = GraphExecutor._stream_node

    async def _spy_stream_node(self, nid, context, queue):
        return await original_stream_node(self, nid, context, _PutSpyQueue(queue))

    events: list[Any] = []

    async def _drive() -> None:
        nonlocal consumed_tool_call_start_at
        async for ev in executor.invoke([]):
            if _is_race_tool_call_start(ev):
                # Stand-in for dispatch.py's translate_stream_event +
                # writer.append() taking real time.
                await asyncio.sleep(0.05)
                consumed_tool_call_start_at = time.monotonic()
            events.append(ev)

    with mock.patch.object(GraphExecutor, "_stream_node", _spy_stream_node):
        await asyncio.wait_for(_drive(), timeout=10.0)

    assert consumed_tool_call_start_at is not None, "race worker never emitted ToolCallStart"
    assert barrier_resolved_at is not None, "barrier never resolved"
    assert barrier_resolved_at >= consumed_tool_call_start_at, (
        "barrier resolved before the prior event finished draining — "
        "the ordering guarantee is broken"
    )

    # Multi-node sanity: both fan-out siblings actually ran to completion,
    # concurrently, sharing the one queue the barrier raced on.
    tool_call_starts = [ev for ev in events if _is_race_tool_call_start(ev)]
    assert len(tool_call_starts) == 1
    text_deltas = [
        ev for ev in events
        if isinstance(ev, ExtendedEvent)
        and isinstance(ev.extended, _GraphNodeEvent)
        and ev.extended.inner_type == "text_delta"
    ]
    assert len(text_deltas) >= 1, "plain sibling never ran concurrently"


@pytest.mark.asyncio
async def test_barrier_awaiting_node_is_cancelled_on_teardown() -> None:
    """An aborted superstep must not leave a barrier-awaiting node hanging.

    If the consumer of ``invoke()`` stops draining early (breaks out of its
    ``async for`` / closes the generator) while a node task is suspended on
    ``await_tool_dispatch_barrier``'s future — i.e. its barrier sentinel is
    still sitting undrained in the queue, and the drain loop never reaches
    it — that node task must still get cancelled, not left awaiting a
    future nothing will ever resolve.

    ``invoke()`` does a bare ``async for ev in self._run_superstep_loop(...):
    yield ev`` with no explicit ``try/finally`` of its own, so closing
    ``invoke()``'s generator does NOT synchronously close
    ``_run_superstep_loop``'s (confirmed separately: closing an outer
    generator that plainly ``async for``s over an inner one does not
    synchronously run the inner one's ``finally`` — CPython drops the
    inner generator via refcounting as part of the outer frame's teardown,
    which schedules the inner's ``aclose()`` through the asyncgen finalizer
    hook, i.e. on a LATER event-loop tick, not in-line). ``_run_superstep_loop``'s
    own ``finally`` (which cancels every node task, "belt-and-braces... if
    the caller closed the iterator early") is exactly what's expected to
    run once that scheduled close happens — this test polls rather than
    asserting immediately after ``aclose()`` returns, since the cancellation
    is real but not synchronous with it.
    """
    llm = _RacingProbeLLM()
    graph = _two_worker_fanout_graph()
    executor = await _build_executor(graph=graph, llm=llm)

    captured_future: "asyncio.Future[None] | None" = None

    class _PutSpyQueue:
        def __init__(self, inner: "asyncio.Queue[Any]") -> None:
            self._inner = inner

        async def put(self, item: Any) -> None:
            await self._inner.put(item)
            if _is_race_tool_call_start(item):
                nonlocal captured_future
                # Post the barrier and await it directly (not via
                # await_tool_dispatch_barrier) so the test keeps its own
                # reference to the future to assert on afterward — the
                # helper's internals are already covered by the race test
                # above; this test is about _run_superstep_loop's teardown,
                # not the helper itself.
                from primer.graph._node_refs import _ToolDispatchBarrier

                fut: "asyncio.Future[None]" = asyncio.get_running_loop().create_future()
                captured_future = fut
                await self._inner.put(_ToolDispatchBarrier(future=fut))
                await fut  # never resolved — the drain loop is aborted below

        async def get(self) -> Any:
            return await self._inner.get()

    original_stream_node = GraphExecutor._stream_node

    async def _spy_stream_node(self, nid, context, queue):
        return await original_stream_node(self, nid, context, _PutSpyQueue(queue))

    with mock.patch.object(GraphExecutor, "_stream_node", _spy_stream_node):
        agen = executor.invoke([])
        found = False
        async for ev in agen:
            if _is_race_tool_call_start(ev):
                found = True
                break
        assert found, "race worker never emitted ToolCallStart"

        # Abort the superstep early: the racing node's task is parked on
        # its barrier future; its sentinel is still undrained in the
        # queue. This is the teardown path under test.
        await asyncio.wait_for(agen.aclose(), timeout=5.0)

        # The cancellation this relies on is real but not synchronous with
        # aclose() returning (see docstring) — poll for it instead of
        # asserting immediately.
        deadline = asyncio.get_event_loop().time() + 5.0
        while asyncio.get_event_loop().time() < deadline:
            if captured_future is not None and captured_future.done():
                break
            await asyncio.sleep(0.01)

    assert captured_future is not None
    assert captured_future.cancelled(), (
        "a node parked on the barrier was not cancelled by teardown - it "
        "would hang forever awaiting a future nothing will ever resolve"
    )
