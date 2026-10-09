"""The graph's own failure surfaces carry no credential: the turn log's generic branch and the session ERROR record (#678 review round 1).

``err_text`` also travels outside the stored node state. The turn log of a node that failed with an exception goes through ``to_problem_details`` (the
generic 500 branch), which masked URL credentials only: a Bearer or Basic token an upstream quoted stayed in ``TurnLogFailed.error.detail``, and
``to_problem_details`` serves the session turn logs too. The graph's terminal ``_GraphErrorEvent`` carried the raw text, and it becomes the session's ERROR
record (``GET /v1/sessions/{sid}/messages``); masking it where it is built covers every site that yields one, a ``ChildGraphFailed``'s message included.
"""

from __future__ import annotations

import json

import pytest

from primer.api.routers.compute import _NodeStateOut
from primer.graph._node_refs import _GraphErrorEvent
from primer.graph.executor import GraphExecutor
from primer.model.except_ import PrimerError
from primer.model.graph import (
    Graph,
    GraphNodeMessage,
    GraphThread,
    _AgentNodeRef,
    _BeginNode,
    _EndNode,
    _StaticEdge,
)
from primer.observability.turn_log_writer import to_problem_details
from primer.session.persistence import _CoalesceState, translate_stream_event
from tests.graph.test_executor_error_emission import _build_executor, _InMemoryStorage

URL = "https://svc-user:hunter2pw@gateway.internal/v1/x?api_key=SKSECRET123456"
BEARER = "sk-abcdefgh12345678"
BASIC = "dXNlcjpodW50ZXIycHc="            # base64 of "user:hunter2pw"
LEAKY = f"ConnectError for url '{URL}' (retry with Bearer {BEARER} or Basic {BASIC})"
SECRETS = ("hunter2pw", "SKSECRET123456", BEARER, BASIC)


def _assert_clean(text: str | None) -> None:
    assert text is not None
    for secret in SECRETS:
        assert secret not in text, f"{secret!r} leaked in {text!r}"
    assert "gateway.internal" in text, f"the rest of the message must survive: {text!r}"


class _Writer:
    def __init__(self) -> None:
        self.events: list = []

    async def append(self, event) -> int:
        self.events.append(event)
        return len(self.events)

    async def aclose(self) -> None:
        return None


class _QuotedError(PrimerError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.problem_extensions = {"upstream": message, "attempts": 3}


# ---- to_problem_details -------------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("exc", [RuntimeError(LEAKY), PrimerError(LEAKY), _QuotedError(LEAKY)], ids=["generic", "primer-error", "with-extension"])
def test_a_problem_envelope_masks_bearer_and_basic_tokens_as_well_as_urls(exc) -> None:
    problem = to_problem_details(exc)

    _assert_clean(problem.detail)
    for value in problem.extensions.values():
        if isinstance(value, str):
            assert not any(secret in value for secret in SECRETS), value


# ---- the graph run ------------------------------------------------------------------------------------------------------------------------------


async def _failing_agent_run(error_text: str):
    graph = Graph(
        id="g-agent-fail", description="begin -> work -> end",
        nodes=[_BeginNode(id="b"), _AgentNodeRef(id="work", agent_id="a", input_template="go"), _EndNode(id="e")],
        edges=[_StaticEdge(from_node="b", to_node="work"), _StaticEdge(from_node="work", to_node="e")],
    )

    async def agent_resolver(_agent_id: str):
        raise RuntimeError(error_text)

    async def llm_resolver(_agent):
        raise KeyError("no llm")

    thread_storage = _InMemoryStorage(GraphThread)
    message_storage = _InMemoryStorage(GraphNodeMessage)
    thread = await GraphExecutor.open_thread(graph=graph, thread_storage=thread_storage, title="t")  # type: ignore[arg-type]
    executor = GraphExecutor(
        graph=graph, agent_resolver=agent_resolver, llm_resolver=llm_resolver,  # type: ignore[arg-type]
        thread_storage=thread_storage, message_storage=message_storage, graph_thread_id=thread.id,  # type: ignore[arg-type]
    )
    writers: dict[str, _Writer] = {}

    def factory(node_id: str) -> _Writer:
        writers[node_id] = _Writer()
        return writers[node_id]

    executor._turn_log_factory = factory  # type: ignore[assignment]
    events = [ev async for ev in executor.invoke([])]
    return events, writers


def _error_record_message(event: _GraphErrorEvent) -> str:
    record = translate_stream_event(event, _CoalesceState())
    record = record[0] if isinstance(record, list) else record
    return record.payload["message"]


@pytest.mark.asyncio
async def test_a_failed_agent_nodes_turn_log_detail_carries_no_credentials() -> None:
    _events, writers = await _failing_agent_run(LEAKY)

    failed = [ev for ev in writers["work"].events if type(ev).__name__ == "TurnLogFailed"]
    assert failed, "the node's failure should be in its turn log"
    _assert_clean(failed[0].error.detail)


def test_a_graph_error_event_masks_its_message_wherever_it_is_built() -> None:
    event = _GraphErrorEvent(code="tool_execution_failed", message=LEAKY, node_id="n")

    _assert_clean(event.message)
    assert event.code == "tool_execution_failed" and event.node_id == "n"


def test_a_graph_error_event_without_a_credential_is_unchanged() -> None:
    event = _GraphErrorEvent(code="routing_failed", message="no edge matched node 'x'", node_id="x")

    assert event.message == "no edge matched node 'x'"


@pytest.mark.asyncio
async def test_a_failed_end_nodes_error_event_and_record_carry_no_credentials() -> None:
    """The End node's schema error quotes the rendered value, so a token in the value reaches the graph's own ERROR record."""
    value = f"{URL} Bearer {BEARER} Basic {BASIC}"
    graph = Graph(
        id="g-bad-end", description="begin -> end",
        nodes=[_BeginNode(id="b"), _EndNode(id="e", output_template=json.dumps(value), output_schema={"type": "object"})],
        edges=[_StaticEdge(from_node="b", to_node="e")],
    )
    executor, thread, thread_storage = await _build_executor(graph=graph)
    events = [ev async for ev in executor.invoke([])]

    errors = [ev for ev in events if isinstance(ev, _GraphErrorEvent)]
    assert errors
    for event in errors:
        _assert_clean(event.message)
        _assert_clean(_error_record_message(event))
    saved = await thread_storage.get(thread.id)
    _assert_clean(saved.node_states["e"].error)


# ---- the node-state API model -------------------------------------------------------------------------------------------------------------------


def test_the_node_state_row_served_by_the_run_view_masks_an_old_runs_error() -> None:
    """A run recorded before the fix keeps its raw text in the workspace; the API model masks it on the way out."""
    row = _NodeStateOut(node_id="work", kind="agent", status="failed", error=LEAKY)

    _assert_clean(row.error)


def test_a_node_state_row_without_an_error_is_unchanged() -> None:
    assert _NodeStateOut(node_id="work", kind="agent", status="done").error is None
