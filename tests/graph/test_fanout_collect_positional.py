"""Regression: fan-out aggregator must preserve POSITIONAL alignment.

BUG 1: the per-instance aggregator write-back used to compact the list
with ``[x for x in agg_list if x is not None]``. For fan-out counts >= 11
the synthesized instance ids sort lexicographically (``worker[10]`` before
``worker[2]``), so instances complete out of numeric index order within a
single superstep. Compaction at write-back then collapsed the transient
None placeholders and SHIFTED every later result, silently dropping
some workers' outputs and mis-positioning others.

Begin -> FanOut(broadcast worker count=12) -> FanIn -> End

Each worker echoes its ``fanout_index``. The FanIn renders one token per
slot. The correct aggregate is ``W0 W1 ... W11`` in order; a compacting
aggregator loses/reorders entries.
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
    FanOutSpec,
    Graph,
    GraphNodeMessage,
    GraphThread,
    _AgentNodeRef,
    _BeginNode,
    _EndNode,
    _FanInNode,
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
    """Echoes the last user-message text back as a single TextDelta."""

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
        yield TextDelta(text=text, index=0)
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


async def _build_executor(*, graph: Graph, llm: _EchoLLM):
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
async def test_fanout_collect_preserves_positional_alignment() -> None:
    count = 12
    graph = Graph.model_construct(
        id="g-positional",
        description="Begin -> FanOut(broadcast count=12) -> FanIn -> End",
        nodes=[
            _BeginNode(id="begin"),
            _FanOutNode(
                id="fan",
                specs=[
                    FanOutSpec(
                        kind="broadcast",
                        target_node_id="worker",
                        count=count,
                    ),
                ],
            ),
            _AgentNodeRef(
                id="worker",
                agent_id="ag",
                input_template="{{ fanout_index }}",
            ),
            _FanInNode(
                id="agg",
                aggregate_template=(
                    "{% for n in nodes.worker %}[{{ n.text }}]{% endfor %}"
                ),
            ),
            _EndNode(id="end", output_template="{{ nodes.agg.text }}"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="fan"),
            _StaticEdge(from_node="worker", to_node="agg"),
            _StaticEdge(from_node="agg", to_node="end"),
        ],
        max_iterations=10,
        harness_id=None,
    )
    llm = _EchoLLM()
    executor, thread, thread_storage = await _build_executor(graph=graph, llm=llm)

    events = await _drain(executor.invoke([]))

    final = await thread_storage.get(thread.id)
    assert final is not None
    assert final.ended_reason == "completed", (
        f"expected completed; got {final.ended_reason!r} / {final.ended_detail!r}"
    )
    assert len(llm.calls) == count

    end_events = [ev for ev in events if isinstance(ev, _GraphEndOutputEvent)]
    assert end_events, "expected an End output event"
    end_text = end_events[-1].text
    expected = "".join(f"[{i}]" for i in range(count))
    assert end_text == expected, (
        f"fan-out aggregate lost positional alignment.\n"
        f"  expected: {expected!r}\n"
        f"  actual:   {end_text!r}"
    )
