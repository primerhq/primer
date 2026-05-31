"""End-to-end ToolCall dispatch: Begin -> ToolCall -> End.

Uses a stub dispatcher injected at the storage-backed ``GraphExecutor``'s
constructor (mirroring tests/graph/test_executor.py's fixture pattern).
The stub records the dispatched ``ToolCallPart`` and returns a fixed
``ToolResultPart``; the test asserts the NodeOutput.text matches and
that dispatch was called with the resolved arguments.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Generic, TypeVar

import pytest

from primer.graph.executor import GraphExecutor
from primer.graph.router import RouterRegistry
from primer.model.agent import Agent
from primer.model.chat import (
    Message,
    StreamEvent,
    ToolCallPart,
    ToolResultPart,
)
from primer.model.common import Identifiable
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.graph import (
    Graph,
    GraphNodeMessage,
    GraphThread,
    NodeOutput,
    _BeginNode,
    _EndNode,
    _StaticEdge,
    _ToolCallNode,
)
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


async def _drain(it: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    return [ev async for ev in it]


@pytest.mark.asyncio
async def test_toolcall_dispatch_e2e() -> None:
    """Begin -> ToolCall(web__search) -> End with a stub dispatcher.

    Asserts the dispatcher saw the resolved arguments and the End-node
    NodeOutput downstream sees ``result.output`` as the ToolCall's text.
    """
    graph = Graph(
        id="g-toolcall",
        description="begin -> tool -> end",
        nodes=[
            _BeginNode(id="begin"),
            _ToolCallNode(
                id="t",
                tool_id="web__search",
                arguments={"query": "hello", "limit": 5},
            ),
            _EndNode(id="exit", output_template="{{ nodes.t.text }}"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="t"),
            _StaticEdge(from_node="t", to_node="exit"),
        ],
    )

    seen_calls: list[tuple[str, dict]] = []

    async def stub_dispatcher(node, arguments):
        seen_calls.append((node.tool_id, dict(arguments)))
        return ToolResultPart(id="tc-stub", output="stub-output")

    async def agent_resolver(agent_id: str) -> Agent:
        raise KeyError(agent_id)

    async def llm_resolver(agent):
        raise NotImplementedError

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
        tool_dispatcher=stub_dispatcher,
    )

    events = await _drain(executor.invoke([]))

    # Dispatcher saw the resolved arguments.
    assert seen_calls == [("web__search", {"query": "hello", "limit": 5})]

    # Graph ended cleanly with the End-node's output_template wired to
    # the ToolCall's NodeOutput.text.
    from primer.graph.base import _GraphEndOutputEvent

    end_outputs = [e for e in events if isinstance(e, _GraphEndOutputEvent)]
    assert len(end_outputs) == 1
    assert end_outputs[0].text == "stub-output"

    loaded = await thread_storage.get(thread.id)
    assert loaded is not None
    assert loaded.ended_reason == "completed"


@pytest.mark.asyncio
async def test_toolcall_template_error_in_args() -> None:
    """A Jinja-undefined arg yields ``ended_detail='template_error'``."""
    graph = Graph(
        id="g-toolcall-tpl-err",
        description="begin -> tool(bad jinja) -> end",
        nodes=[
            _BeginNode(id="begin"),
            _ToolCallNode(
                id="t",
                tool_id="web__search",
                arguments={"q": "{{ nodes.absent.text }}"},
            ),
            _EndNode(id="exit"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="t"),
            _StaticEdge(from_node="t", to_node="exit"),
        ],
    )

    async def stub_dispatcher(node, arguments):  # pragma: no cover — never reached
        raise AssertionError("dispatcher should not be called when args resolution fails")

    async def agent_resolver(agent_id: str) -> Agent:
        raise KeyError(agent_id)

    async def llm_resolver(agent):
        raise NotImplementedError

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
        tool_dispatcher=stub_dispatcher,
    )

    await _drain(executor.invoke([]))

    loaded = await thread_storage.get(thread.id)
    assert loaded is not None
    assert loaded.ended_reason == "failed"
    assert loaded.ended_detail == "template_error"


@pytest.mark.asyncio
async def test_toolcall_dispatcher_raises_maps_to_tool_execution_failed() -> None:
    """A dispatcher exception ends the graph with ``tool_execution_failed``."""
    graph = Graph(
        id="g-toolcall-fail",
        description="begin -> tool(boom) -> end",
        nodes=[
            _BeginNode(id="begin"),
            _ToolCallNode(
                id="t",
                tool_id="web__search",
                arguments={"q": "x"},
            ),
            _EndNode(id="exit"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="t"),
            _StaticEdge(from_node="t", to_node="exit"),
        ],
    )

    async def stub_dispatcher(node, arguments):
        raise RuntimeError("tool blew up")

    async def agent_resolver(agent_id: str) -> Agent:
        raise KeyError(agent_id)

    async def llm_resolver(agent):
        raise NotImplementedError

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
        tool_dispatcher=stub_dispatcher,
    )

    await _drain(executor.invoke([]))

    loaded = await thread_storage.get(thread.id)
    assert loaded is not None
    assert loaded.ended_reason == "failed"
    assert loaded.ended_detail == "tool_execution_failed"
