"""A graph node's failure text carries no credential, wherever the run keeps it (security ticket 01a11fbc-ec5a).

``err_text = str(done.error)`` is whatever the node's exception or error message printed: a tool's httpx error holds the request URL whole, a schema
error quotes the value that failed, and a routing or template error quotes its input. The text was stored raw in ``NodeRuntimeState.error`` (the
``GraphThread.node_states`` row, served by ``GET /v1/graphs/{gid}/runs/{rid}/node_states``, and the workspace's ``state.json``, which is git-committed
and so kept in history), in the graph turn log's failed entry (the pre-stringified branch) and, for a fan-out collected failure, in
``NodeOutput.error``, which a FanIn template renders into the next prompt. Both models now redact the field (URL credentials, Bearer and Basic
tokens) so every producer is covered, and the turn log's pre-stringified branch redacts its detail.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from primer.graph.executor import GraphExecutor
from primer.model.graph import (
    FanOutSpec,
    Graph,
    GraphNodeMessage,
    GraphThread,
    NodeOutput,
    NodeRuntimeState,
    NodeRuntimeStatus,
    _AgentNodeRef,
    _BeginNode,
    _EndNode,
    _FanInNode,
    _FanOutNode,
    _StaticEdge,
)
from tests.graph.test_executor_error_emission import _build_executor, _drain, _InMemoryStorage
from tests.graph.test_fanout_collect import _build_executor as _build_collect_executor
from tests.graph.test_fanout_collect import _FailingFakeLLM
from tests.graph.test_workspace_executor import _agent, _build_executor as _build_workspace_executor
from tests.graph.test_workspace_executor import _drain as _drain_workspace
from tests.graph.test_workspace_executor import _FakeLLM, _make_state_repo

URL = "https://svc-user:hunter2pw@gateway.internal/v1/x?api_key=SKSECRET123456"
LEAKY = f"ConnectError for url '{URL}' (retry with Bearer sk-abcdefgh12345678)"
SECRETS = ("hunter2pw", "SKSECRET123456", "sk-abcdefgh12345678")


def _assert_clean(text: str | None) -> None:
    assert text is not None
    for secret in SECRETS:
        assert secret not in text, f"{secret!r} leaked in {text!r}"
    assert "gateway.internal" in text, f"the rest of the message must survive: {text!r}"


def _end_node_with_a_bad_value() -> Graph:
    """The End node's rendered value is a JSON string, which the object schema refuses with a message that quotes the value."""
    return Graph(
        id="g-bad-end", description="begin -> end",
        nodes=[_BeginNode(id="b"), _EndNode(id="e", output_template=json.dumps(URL), output_schema={"type": "object"})],
        edges=[_StaticEdge(from_node="b", to_node="e")],
    )


class _Writer:
    def __init__(self) -> None:
        self.events: list = []

    async def append(self, event) -> int:
        self.events.append(event)
        return len(self.events)

    async def aclose(self) -> None:
        return None


# ---- the models: every producer is covered ---------------------------------------------------------------------------------------------------------


def test_a_node_states_error_is_stored_without_credentials():
    state = NodeRuntimeState(status=NodeRuntimeStatus.FAILED, error=LEAKY)

    _assert_clean(state.error)


def test_a_node_outputs_error_is_stored_without_credentials():
    output = NodeOutput(text="", iteration=0, error=LEAKY, ended_detail="node_failed")

    _assert_clean(output.error)


def test_a_credential_free_error_and_no_error_are_stored_as_they_were():
    plain = "output is not JSON: Expecting value: line 1 column 1 (char 0), see https://example.com/docs?page=2"

    assert NodeRuntimeState(error=plain).error == plain
    assert NodeRuntimeState().error is None
    assert NodeOutput(text="", iteration=0, error=plain).error == plain
    assert NodeOutput(text="", iteration=0).error is None


# ---- through a run ---------------------------------------------------------------------------------------------------------------------------


async def _agent_node_that_fails_with(error_text: str):
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
    await _drain(executor.invoke([]))
    return await thread_storage.get(thread.id)


@pytest.mark.asyncio
async def test_a_failed_nodes_row_in_node_states_carries_no_credentials():
    saved = await _agent_node_that_fails_with(LEAKY)

    assert saved.node_states["work"].status == NodeRuntimeStatus.FAILED
    _assert_clean(saved.node_states["work"].error)


@pytest.mark.asyncio
async def test_a_failed_end_node_is_stored_and_logged_without_credentials():
    """The End node's schema error quotes the value; its failure is a pre-stringified one, so it reaches the turn log through the generic 500 branch."""
    executor, thread, thread_storage = await _build_executor(graph=_end_node_with_a_bad_value())
    writers: dict[str, _Writer] = {}

    def factory(node_id: str) -> _Writer:
        writers[node_id] = _Writer()
        return writers[node_id]

    executor._turn_log_factory = factory  # type: ignore[assignment]
    await _drain(executor.invoke([]))

    saved = await thread_storage.get(thread.id)
    _assert_clean(saved.node_states["e"].error)
    failed = [ev for ev in writers["e"].events if type(ev).__name__ == "TurnLogFailed"]
    assert failed, "the End node's failure should be in its turn log"
    _assert_clean(failed[0].error.detail)


@pytest.mark.asyncio
async def test_state_json_and_the_git_history_of_a_failed_run_carry_no_credentials(tmp_path: Path):
    repo = await _make_state_repo(tmp_path)
    executor = await _build_workspace_executor(
        graph=_end_node_with_a_bad_value(), llm=_FakeLLM(scripts=[[]]), state_repo=repo, graph_session_id="gsid-leak", agents={"x": _agent("x")},
    )

    await _drain_workspace(executor.invoke([]))

    state = json.loads((executor.state_root / "state.json").read_text(encoding="utf-8"))
    _assert_clean(state["node_states"]["e"]["error"])
    history = subprocess.check_output(["git", "-C", str(repo.path), "log", "-p", "--all", "--format=%B"], text=True)
    for secret in SECRETS:
        assert secret not in history, f"{secret!r} is in the committed history"


class _LeakyFailingLLM(_FailingFakeLLM):
    async def _stream_fail(self, text):
        if False:
            yield  # pragma: no cover
        raise RuntimeError(LEAKY)


@pytest.mark.asyncio
async def test_a_fanin_that_renders_a_collected_failure_does_not_pass_the_credentials_on():
    graph = Graph.model_construct(
        id="g-collect-leak", description="begin -> fanout(collect) -> fanin -> end",
        nodes=[
            _BeginNode(id="begin"),
            _FanOutNode(id="fan", specs=[FanOutSpec(kind="broadcast", target_node_id="worker", count=2, on_failure="collect")]),
            _AgentNodeRef(id="worker", agent_id="ag", input_template="W{{ fanout_index }}"),
            _FanInNode(id="agg", aggregate_template="{% for n in nodes.worker %}[{% if n.error %}{{ n.error }}{% else %}{{ n.text }}{% endif %}]{% endfor %}"),
            _EndNode(id="end", output_template="{{ nodes.agg.text }}"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="fan"),
            _StaticEdge(from_node="worker", to_node="agg"),
            _StaticEdge(from_node="agg", to_node="end"),
        ],
    )
    executor, thread, thread_storage = await _build_collect_executor(graph=graph, llm=_LeakyFailingLLM(fail_marker="W1"))

    await _drain(executor.invoke([]))

    context = executor._context
    assert context is not None
    _assert_clean(context.nodes["agg"].text)
