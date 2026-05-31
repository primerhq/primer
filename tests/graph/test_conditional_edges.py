"""Per-operator conditional-edge integration tests.

Spec §3 + §7.3. Drives a small Begin -> Decider -> {End-A, End-B}
graph through the full :class:`GraphExecutor` for every BranchCondition
operator, plus the missing-path-False rule, bracket-index paths,
AND-of-conditions, ``default_to``, and the no-match-no-default failure
mode. Complements ``test_branch_condition_evaluation.py`` (unit-level
predicate semantics) by asserting the executor honours those semantics
when it picks the next ready node.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, Generic, TypeVar

import pytest

from primer.graph.base import _GraphErrorEvent
from primer.graph.executor import GraphExecutor
from primer.model.agent import Agent, AgentModel
from primer.model.chat import Done, Message, StreamEvent, TextDelta
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
from primer.model.workspace_session import SessionStatus


# ===========================================================================
# In-memory storage double (parallel to test_executor.py / test_routing_failed.py)
# ===========================================================================


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


# ===========================================================================
# FakeLLM — scripted parsed-output payload
# ===========================================================================


class _FakeLLM:
    def __init__(self, payload: dict | str) -> None:
        if isinstance(payload, dict):
            self._text = json.dumps(payload)
        else:
            self._text = payload
        self.calls: list[dict[str, Any]] = []

    async def list_models(self):
        return ["m"]

    def stream(self, *, model: str, messages: list[Message], **kwargs: Any):
        self.calls.append({"model": model, "messages": list(messages), **kwargs})
        return self._stream_impl()

    async def _stream_impl(self) -> AsyncIterator[StreamEvent]:
        yield TextDelta(text=self._text, index=0)
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


async def _build_executor(*, graph: Graph, llm: _FakeLLM):
    async def agent_resolver(agent_id: str) -> Agent:
        return _agent(agent_id)

    async def llm_resolver(_a: Agent):
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


def _two_end_graph(
    *,
    branches: list[JsonPathBranch],
    default_to: str | None = None,
    graph_id: str = "g-cond",
) -> Graph:
    """Begin -> decider (agent) -> conditional -> {end_a, end_b}."""
    return Graph(
        id=graph_id,
        description="conditional routing test",
        max_iterations=4,
        nodes=[
            _BeginNode(id="begin"),
            _AgentNodeRef(
                id="decider",
                agent_id="x",
                response_format={"type": "object"},
            ),
            _EndNode(id="end_a"),
            _EndNode(id="end_b"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="decider"),
            _ConditionalEdge(
                from_node="decider",
                router=_JsonPathRouter(
                    branches=branches,
                    default_to=default_to,
                ),
            ),
        ],
    )


# ===========================================================================
# Per-operator parametrised test
# ===========================================================================


# (op, value, parsed-payload-that-routes-to-end_a, parsed-payload-that-routes-to-end_b)
# The branch fires when condition is True; the parsed payload below
# pairs the "matching" value with the operator under test so end_a is
# the chosen target, with default_to=end_b as the catch-all.
_OPERATOR_CASES = [
    ("eq", "win", {"go": "win"}, {"go": "lose"}),
    ("ne", "win", {"go": "other"}, {"go": "win"}),
    ("gt", 50, {"score": 80}, {"score": 10}),
    ("gte", 50, {"score": 50}, {"score": 49}),
    ("lt", 50, {"score": 10}, {"score": 80}),
    ("lte", 50, {"score": 50}, {"score": 51}),
    ("in", ["a", "b"], {"tag": "a"}, {"tag": "c"}),
    ("not_in", ["a", "b"], {"tag": "c"}, {"tag": "a"}),
    ("exists", None, {"flag": True}, {"other": 1}),  # no `flag` key
]


@pytest.mark.parametrize(
    "op,value,parsed_match,parsed_default", _OPERATOR_CASES,
    ids=[c[0] for c in _OPERATOR_CASES],
)
@pytest.mark.asyncio
async def test_operator_routes_to_end_a_on_match(
    op: str, value, parsed_match: dict, parsed_default: dict,
) -> None:
    """For each operator, a parsed payload that satisfies the condition
    routes to end_a (the branch target); a payload that fails sends the
    graph to end_b via ``default_to``. We assert the match case here;
    the default case is asserted in the matching test below."""
    path = "go"
    if op in {"gt", "gte", "lt", "lte"}:
        path = "score"
    elif op in {"in", "not_in"}:
        path = "tag"
    elif op == "exists":
        path = "flag"

    branches = [
        JsonPathBranch(
            conditions=[BranchCondition(path=path, op=op, value=value)],
            to_node="end_a",
        ),
    ]
    graph = _two_end_graph(branches=branches, default_to="end_b")
    llm = _FakeLLM(parsed_match)
    executor, thread, ts = await _build_executor(graph=graph, llm=llm)
    await _drain(executor.invoke([]))

    loaded = await ts.get(thread.id)
    assert loaded is not None, "thread row missing"
    assert loaded.status == SessionStatus.ENDED
    assert loaded.ended_reason == "completed"
    # end_a fired (its node_state is ENDED); end_b never ran.
    assert loaded.node_states["end_a"].status.value == "ended"
    assert loaded.node_states["end_b"].status.value == "pending"


@pytest.mark.asyncio
async def test_missing_path_routes_via_default_to() -> None:
    """A path absent from the parsed payload makes every operator False
    (spec §3.1) — the router falls back to ``default_to``."""
    branches = [
        JsonPathBranch(
            conditions=[BranchCondition(path="go", op="eq", value="win")],
            to_node="end_a",
        ),
    ]
    graph = _two_end_graph(branches=branches, default_to="end_b")
    # Parsed payload has no `go` key.
    llm = _FakeLLM({"other": "stuff"})
    executor, thread, ts = await _build_executor(graph=graph, llm=llm)
    await _drain(executor.invoke([]))

    loaded = await ts.get(thread.id)
    assert loaded is not None
    assert loaded.ended_reason == "completed"
    assert loaded.node_states["end_b"].status.value == "ended"
    assert loaded.node_states["end_a"].status.value == "pending"


@pytest.mark.asyncio
async def test_bracket_index_path_resolves() -> None:
    """``items[2].name`` walks the bracketed index then a dotted segment
    inside the resolved element (spec §3.2 path syntax)."""
    branches = [
        JsonPathBranch(
            conditions=[
                BranchCondition(path="items[2].name", op="eq", value="charlie"),
            ],
            to_node="end_a",
        ),
    ]
    graph = _two_end_graph(branches=branches, default_to="end_b")
    llm = _FakeLLM(
        {
            "items": [
                {"name": "alice"},
                {"name": "bob"},
                {"name": "charlie"},
            ],
        }
    )
    executor, thread, ts = await _build_executor(graph=graph, llm=llm)
    await _drain(executor.invoke([]))

    loaded = await ts.get(thread.id)
    assert loaded is not None
    assert loaded.node_states["end_a"].status.value == "ended"


@pytest.mark.asyncio
async def test_and_of_conditions_all_must_hold() -> None:
    """Multiple conditions in one branch AND together. With only one
    satisfied, the branch fails to match and ``default_to`` fires."""
    branches = [
        JsonPathBranch(
            conditions=[
                BranchCondition(path="ok", op="eq", value=True),
                BranchCondition(path="score", op="gte", value=80),
            ],
            to_node="end_a",
        ),
    ]
    graph = _two_end_graph(branches=branches, default_to="end_b")

    # First payload satisfies both -> end_a.
    llm_ok = _FakeLLM({"ok": True, "score": 90})
    executor, thread, ts = await _build_executor(graph=graph, llm=llm_ok)
    await _drain(executor.invoke([]))
    loaded = await ts.get(thread.id)
    assert loaded is not None
    assert loaded.node_states["end_a"].status.value == "ended"

    # Second payload misses the score gate -> default_to=end_b.
    llm_partial = _FakeLLM({"ok": True, "score": 10})
    executor2, thread2, ts2 = await _build_executor(graph=graph, llm=llm_partial)
    await _drain(executor2.invoke([]))
    loaded2 = await ts2.get(thread2.id)
    assert loaded2 is not None
    assert loaded2.node_states["end_b"].status.value == "ended"
    assert loaded2.node_states["end_a"].status.value == "pending"


@pytest.mark.asyncio
async def test_default_to_fires_when_no_branch_matches() -> None:
    """A parsed payload that fails every branch routes to ``default_to``."""
    branches = [
        JsonPathBranch(
            conditions=[BranchCondition(path="go", op="eq", value="win")],
            to_node="end_a",
        ),
    ]
    graph = _two_end_graph(branches=branches, default_to="end_b")
    llm = _FakeLLM({"go": "neither"})
    executor, thread, ts = await _build_executor(graph=graph, llm=llm)
    await _drain(executor.invoke([]))
    loaded = await ts.get(thread.id)
    assert loaded is not None
    assert loaded.ended_reason == "completed"
    assert loaded.node_states["end_b"].status.value == "ended"


@pytest.mark.asyncio
async def test_no_match_no_default_to_emits_routing_failed() -> None:
    """When no branch matches and ``default_to`` is unset, the graph
    ends with ``ended_reason='failed'`` + ``ended_detail='routing_failed'``
    and a ``_GraphErrorEvent`` is emitted (spec §5.4). Overlaps with
    Phase 3 Task 3.6's coverage; reuse the pattern here to keep this
    file's matrix complete."""
    # Single End topology — the no-default case can't reference end_b
    # because the topology validator would flag it unreachable.
    graph = Graph(
        id="g-no-default",
        description="single End, conditional with no default_to",
        max_iterations=4,
        nodes=[
            _BeginNode(id="begin"),
            _AgentNodeRef(
                id="decider",
                agent_id="x",
                response_format={"type": "object"},
            ),
            _EndNode(id="end_a"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="decider"),
            _ConditionalEdge(
                from_node="decider",
                router=_JsonPathRouter(
                    branches=[
                        JsonPathBranch(
                            conditions=[
                                BranchCondition(path="go", op="eq", value="win"),
                            ],
                            to_node="end_a",
                        ),
                    ],
                ),
            ),
        ],
    )
    llm = _FakeLLM({"go": "miss"})
    executor, thread, ts = await _build_executor(graph=graph, llm=llm)
    events = await _drain(executor.invoke([]))

    err_events = [ev for ev in events if isinstance(ev, _GraphErrorEvent)]
    assert err_events, "expected a _GraphErrorEvent on no-match-no-default"
    assert err_events[0].code == "routing_failed"

    loaded = await ts.get(thread.id)
    assert loaded is not None
    assert loaded.status == SessionStatus.ENDED
    assert loaded.ended_reason == "failed"
    assert loaded.ended_detail == "routing_failed"
