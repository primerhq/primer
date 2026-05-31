"""FanOutSpec.on_failure='drain_then_fail' — workers run to completion before
the graph terminates failed.

Begin -> FanOut(broadcast worker count=3, on_failure='drain_then_fail') -> End

Worker[1] raises; workers 0 and 2 succeed. After all three workers have
completed:

* ``context.nodes['worker[0]']`` and ``context.nodes['worker[2]']`` carry
  the successful workers' NodeOutputs.
* The graph terminates ``failed`` with
  ``ended_detail='fanin_upstream_failed'``.
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
async def test_drain_then_fail_completes_siblings_then_terminates() -> None:
    """Begin -> FanOut(broadcast count=3, on_failure='drain_then_fail') -> End:
    worker[1] raises; workers 0/2 still produce their NodeOutputs; graph
    terminates ``failed`` with ``ended_detail='fanin_upstream_failed'``.
    """
    graph = Graph.model_construct(
        id="g-drain-then-fail",
        description="Begin -> FanOut(broadcast drain_then_fail) -> End",
        nodes=[
            _BeginNode(id="begin"),
            _FanOutNode(
                id="fan",
                specs=[
                    FanOutSpec(
                        kind="broadcast",
                        target_node_id="worker",
                        count=3,
                        on_failure="drain_then_fail",
                    ),
                ],
            ),
            _AgentNodeRef(
                id="worker",
                agent_id="ag",
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

    # Capture each context snapshot at the start of every per-node result
    # phase by patching _save_state (called twice per superstep — before
    # and after the per-node loop). The "after" snapshot lets us observe
    # the worker outputs that landed before termination.
    captured_node_states: list[dict] = []
    original_save_state = executor._save_state

    async def tap_save(*, iteration, node_states, status, ended_reason=None, ended_detail=None):
        captured_node_states.append(dict(node_states))
        return await original_save_state(
            iteration=iteration,
            node_states=node_states,
            status=status,
            ended_reason=ended_reason,
            ended_detail=ended_detail,
        )

    executor._save_state = tap_save  # type: ignore[method-assign]

    events = await _drain(executor.invoke([]))

    # Final thread state carries fanin_upstream_failed.
    final = await thread_storage.get(thread.id)
    assert final is not None
    assert final.ended_reason == "failed"
    assert final.ended_detail == "fanin_upstream_failed"

    # Workers 0 and 2 succeeded (3 total LLM calls including the failed one).
    assert len(llm.calls) == 3

    # The error event chain should mention the failed worker.
    err_events = [ev for ev in events if isinstance(ev, _GraphErrorEvent)]
    assert err_events, "expected at least one terminal _GraphErrorEvent"
    assert err_events[-1].code == "fanin_upstream_failed"
    assert err_events[-1].node_id == "worker[1]"

    # The successful workers' NodeOutputs landed in node_states; the failing
    # worker is FAILED. Look at the last superstep's node_states snapshot.
    from primer.model.graph import NodeRuntimeStatus
    last_states = captured_node_states[-1]
    assert last_states["worker[0]"].status == NodeRuntimeStatus.ENDED
    assert last_states["worker[2]"].status == NodeRuntimeStatus.ENDED
    assert last_states["worker[1]"].status == NodeRuntimeStatus.FAILED
