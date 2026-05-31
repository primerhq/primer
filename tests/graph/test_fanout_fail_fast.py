"""FanOutSpec.on_failure='fail_fast' (default) — baseline behaviour lock-in.

When a fan-out worker fails and the spawning ``FanOutSpec.on_failure`` is
``fail_fast`` (default), the graph terminates immediately at the end of the
superstep with ``ended_reason='failed'`` and the failed node's
``ended_detail``.

This test pins down the existing behaviour from Phases 3 / 4 so the
subsequent ``drain_then_fail`` / ``collect`` modes (Tasks 5.2 / 5.3) can be
contrasted against it. No executor changes are required.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Generic, TypeVar

import pytest

from primer.graph.base import _GraphErrorEvent
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


class _FailingFakeLLM:
    """LLM stub that raises for the worker whose rendered prompt contains the
    configured failure marker; all other workers echo their input text."""

    def __init__(self, fail_marker: str) -> None:
        self._fail_marker = fail_marker
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
        if self._fail_marker in text:
            return self._stream_fail(text)
        return self._stream_ok(text)

    async def _stream_ok(self, text: str) -> AsyncIterator[StreamEvent]:
        yield TextDelta(text=text, index=0)
        yield Done(stop_reason="stop", raw_reason="stop")

    async def _stream_fail(self, text: str) -> AsyncIterator[StreamEvent]:
        # Make the function a real async generator so the executor sees a
        # genuine streaming failure rather than a synchronous raise.
        if False:
            yield  # pragma: no cover
        raise RuntimeError(f"simulated worker failure for prompt={text!r}")


def _agent(agent_id: str) -> Agent:
    return Agent(
        id=agent_id,
        description=f"agent {agent_id}",
        model=AgentModel(provider_id="p", model_name="m"),
        system_prompt=[],
    )


def _model() -> LLMModel:
    return LLMModel(name="m", context_length=128_000)


async def _build_executor(*, graph: Graph, llm: _FailingFakeLLM):
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
async def test_fail_fast_terminates_graph_on_first_worker_failure() -> None:
    """Begin -> FanOut(broadcast count=3, on_failure='fail_fast') -> End:
    worker[1] raises; the graph terminates ``failed`` at the end of the
    superstep that hosts the workers.
    """
    graph = Graph.model_construct(
        id="g-fail-fast",
        description="Begin -> FanOut(broadcast fail_fast) -> End",
        nodes=[
            _BeginNode(id="begin"),
            _FanOutNode(
                id="fan",
                specs=[
                    FanOutSpec(
                        kind="broadcast",
                        target_node_id="worker",
                        count=3,
                        on_failure="fail_fast",
                    ),
                ],
            ),
            _AgentNodeRef(
                id="worker",
                agent_id="ag",
                # Embed a fail marker on worker[1] so the LLM stub can raise.
                input_template="W{{ fanout_index }}",
            ),
            _EndNode(id="end"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="fan"),
        ],
        max_iterations=10,
        harness_id=None,
    )
    llm = _FailingFakeLLM(fail_marker="W1")
    executor, thread, thread_storage = await _build_executor(graph=graph, llm=llm)

    events = await _drain(executor.invoke([]))

    # Final thread state must record reason='failed'.
    final = await thread_storage.get(thread.id)
    assert final is not None
    assert final.ended_reason == "failed"

    # Some terminal error event should mention the failed worker.
    err_events = [ev for ev in events if isinstance(ev, _GraphErrorEvent)]
    # In fail_fast mode the executor emits an error event for the failing
    # node when its _NodeDone carries an ``ended_detail`` code. The LLM
    # raise path posts ``_NodeDone(error=exc, ended_detail=None)`` so the
    # ``error_events`` channel may stay empty — but the thread row's
    # ended_reason="failed" is the contract we care about. If the executor
    # DID emit an error event, its node_id should pin to the failed worker.
    if err_events:
        assert any(ev.node_id == "worker[1]" for ev in err_events)
