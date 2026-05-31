"""Spec B §1.4 ``ended_detail`` codes reach the session as ``error``
``SessionMessageRecord``s with the expected payload shape.

Most wiring exists from Phases 3/5/6 — this is the explicit end-to-end
test that for every new Spec B code:

* the WorkspaceGraphExecutor emits a ``_GraphErrorEvent`` with that code,
* the session-layer translator (``translate_stream_event``) turns it
  into a ``SessionMessageRecord(kind=ERROR, payload={code, message,
  node_id, path})``,
* the executor's persisted state carries the same ``ended_detail``.

Covers:

* ``tool_output_invalid``      — ToolCall output doesn't match output_schema
* ``tool_execution_failed``    — dispatcher raises RuntimeError
* ``fanout_source_invalid``    — FanOut map source doesn't resolve to a list
* ``fanin_upstream_failed``    — drain_then_fail with one sibling failing
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from primer.graph.base import _GraphErrorEvent
from primer.graph.workspace_executor import WorkspaceGraphExecutor
from primer.model.agent import Agent, AgentModel
from primer.model.chat import (
    Done,
    Message,
    StreamEvent,
    TextDelta,
    ToolResultPart,
)
from primer.model.graph import (
    FanOutSpec,
    Graph,
    NodeOutput,
    _AgentNodeRef,
    _BeginNode,
    _EndNode,
    _FanInNode,
    _FanOutNode,
    _StaticEdge,
    _ToolCallNode,
)
from primer.model.provider import LLMModel
from primer.model.workspace_session import SessionMessageKind
from primer.session.persistence import _CoalesceState, translate_stream_event
from primer.workspace.local.state import LocalStateRepo as StateRepo


# ===========================================================================
# Test scaffolding
# ===========================================================================


class _ScriptedLLM:
    """Stream-events LLM stub. Each call replays the next script (or the last
    one if scripts are exhausted)."""

    def __init__(self, *, scripts: list[list[StreamEvent]]) -> None:
        self._scripts = scripts
        self._cursor = 0

    async def list_models(self):
        return ["m"]

    def stream(
        self,
        *,
        model: str,
        messages: list[Message],
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        idx = min(self._cursor, len(self._scripts) - 1)
        self._cursor += 1
        return self._impl(self._scripts[idx])

    async def _impl(self, events: list[StreamEvent]) -> AsyncIterator[StreamEvent]:
        for ev in events:
            yield ev


def _agent(agent_id: str) -> Agent:
    return Agent(
        id=agent_id,
        description=f"agent {agent_id}",
        model=AgentModel(provider_id="p", model_name="m"),
    )


def _model() -> LLMModel:
    return LLMModel(name="m", context_length=128_000)


async def _make_state_repo(tmp_path: Path) -> StateRepo:
    repo = StateRepo(tmp_path / ".state", workspace_id="ws-spec-b-codes")
    await repo.initialize()
    return repo


async def _drain(it: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    return [ev async for ev in it]


class _TestExecutor(WorkspaceGraphExecutor):
    """WorkspaceGraphExecutor subclass that lets tests inject a fake
    tool-dispatch callable without building a full ToolExecutionManager.
    """

    def __init__(self, *, tool_dispatch=None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._test_tool_dispatch = tool_dispatch

    async def _dispatch_toolcall(
        self,
        node: "_ToolCallNode",
        arguments: dict[str, Any],
        *,
        bypass_approval: bool = False,
    ) -> ToolResultPart:
        if self._test_tool_dispatch is None:
            return await super()._dispatch_toolcall(
                node, arguments, bypass_approval=bypass_approval,
            )
        return await self._test_tool_dispatch(node, arguments)


async def _build_test_executor(
    *,
    graph: Graph,
    tmp_path: Path,
    graph_session_id: str,
    llm: _ScriptedLLM | None = None,
    tool_dispatch=None,
    graph_input: Any = None,
) -> _TestExecutor:
    repo = await _make_state_repo(tmp_path)

    async def agent_resolver(agent_id: str) -> Agent:
        return _agent(agent_id)

    async def llm_resolver(_a: Agent):
        return (llm or _ScriptedLLM(scripts=[[]]), _model())

    return _TestExecutor(
        graph=graph,
        agent_resolver=agent_resolver,
        llm_resolver=llm_resolver,  # type: ignore[arg-type]
        state_repo=repo,
        graph_session_id=graph_session_id,
        graph_input=graph_input,
        tool_dispatch=tool_dispatch,
    )


def _terminal_error(events: list[StreamEvent]) -> _GraphErrorEvent:
    errs = [e for e in events if isinstance(e, _GraphErrorEvent)]
    assert errs, f"expected at least one _GraphErrorEvent in {events!r}"
    return errs[-1]


def _translate_to_record(ev: _GraphErrorEvent):
    rec = translate_stream_event(ev, _CoalesceState())  # type: ignore[arg-type]
    assert rec is not None
    assert not isinstance(rec, list)
    return rec


# ===========================================================================
# Scenarios
# ===========================================================================


@pytest.mark.asyncio
async def test_tool_output_invalid_reaches_session_as_error_record(
    tmp_path: Path,
) -> None:
    """ToolCall with output_schema that the dispatcher's output doesn't match
    -> _GraphErrorEvent(code='tool_output_invalid') -> ERROR record."""
    graph = Graph(
        id="g-tool-output-invalid",
        description="begin -> tool(bad schema) -> end",
        nodes=[
            _BeginNode(id="begin"),
            _ToolCallNode(
                id="t",
                tool_id="fake__echo",
                arguments={"q": "x"},
                output_schema={"type": "object", "required": ["needed"]},
            ),
            _EndNode(id="exit"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="t"),
            _StaticEdge(from_node="t", to_node="exit"),
        ],
    )

    async def stub_dispatcher(node, arguments):
        # Returns valid JSON that fails the schema (no 'needed' key).
        return ToolResultPart(id="tc-1", output='{"other": "value"}')

    executor = await _build_test_executor(
        graph=graph,
        tmp_path=tmp_path,
        graph_session_id="gsid-tool-output",
        tool_dispatch=stub_dispatcher,
    )
    events = await _drain(executor.invoke([]))

    err = _terminal_error(events)
    assert err.code == "tool_output_invalid"

    rec = _translate_to_record(err)
    assert rec.kind == SessionMessageKind.ERROR
    assert rec.payload["code"] == "tool_output_invalid"
    assert rec.payload["node_id"] == "t"
    assert "message" in rec.payload
    assert "path" in rec.payload

    state = await executor.load_state()
    assert state is not None
    assert state["ended_reason"] == "failed"
    assert state["ended_detail"] == "tool_output_invalid"


@pytest.mark.asyncio
async def test_tool_execution_failed_reaches_session_as_error_record(
    tmp_path: Path,
) -> None:
    """ToolCall whose dispatcher raises -> _GraphErrorEvent(code=
    'tool_execution_failed') -> ERROR record."""
    graph = Graph(
        id="g-tool-execution-failed",
        description="begin -> tool(boom) -> end",
        nodes=[
            _BeginNode(id="begin"),
            _ToolCallNode(id="t", tool_id="fake__boom", arguments={"q": "x"}),
            _EndNode(id="exit"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="t"),
            _StaticEdge(from_node="t", to_node="exit"),
        ],
    )

    async def stub_dispatcher(node, arguments):
        raise RuntimeError("tool blew up")

    executor = await _build_test_executor(
        graph=graph,
        tmp_path=tmp_path,
        graph_session_id="gsid-tool-exec",
        tool_dispatch=stub_dispatcher,
    )
    events = await _drain(executor.invoke([]))

    err = _terminal_error(events)
    assert err.code == "tool_execution_failed"

    rec = _translate_to_record(err)
    assert rec.kind == SessionMessageKind.ERROR
    assert rec.payload["code"] == "tool_execution_failed"
    assert rec.payload["node_id"] == "t"

    state = await executor.load_state()
    assert state is not None
    assert state["ended_reason"] == "failed"
    assert state["ended_detail"] == "tool_execution_failed"


@pytest.mark.asyncio
async def test_fanout_source_invalid_reaches_session_as_error_record(
    tmp_path: Path,
) -> None:
    """FanOut map spec whose ``source_path`` resolves to a non-list value
    -> _GraphErrorEvent(code='fanout_source_invalid') -> ERROR record.

    Begin's parsed payload doesn't contain ``items`` -> resolution misses
    -> fanout source resolves to a non-list -> error.
    """
    # Use a Begin node feeding a map FanOut. The map's source is the Begin
    # node itself; source_path 'not_a_list' selects a string value -> invalid.
    graph = Graph(
        id="g-fanout-source-invalid",
        description="begin -> fanout(map non-list) -> worker -> end",
        nodes=[
            _BeginNode(id="begin"),
            _FanOutNode(
                id="fan",
                specs=[
                    FanOutSpec(
                        kind="map",
                        target_node_id="worker",
                        source_node_id="begin",
                        source_path="not_a_list",
                    ),
                ],
            ),
            _AgentNodeRef(id="worker", agent_id="ag"),
            _EndNode(id="exit"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="fan"),
            _StaticEdge(from_node="worker", to_node="exit"),
        ],
    )

    executor = await _build_test_executor(
        graph=graph,
        tmp_path=tmp_path,
        graph_session_id="gsid-fanout-src",
        # Begin's parsed payload is a dict carrying a non-list at the path.
        graph_input={"not_a_list": "i am a string, not a list"},
    )
    events = await _drain(executor.invoke([]))

    err = _terminal_error(events)
    assert err.code == "fanout_source_invalid"

    rec = _translate_to_record(err)
    assert rec.kind == SessionMessageKind.ERROR
    assert rec.payload["code"] == "fanout_source_invalid"

    state = await executor.load_state()
    assert state is not None
    assert state["ended_reason"] == "failed"
    assert state["ended_detail"] == "fanout_source_invalid"


@pytest.mark.asyncio
async def test_fanin_upstream_failed_reaches_session_as_error_record(
    tmp_path: Path,
) -> None:
    """FanOut(broadcast count=2, on_failure='drain_then_fail') where one
    worker fails -> after draining, graph terminates with
    fanin_upstream_failed -> ERROR record."""
    # Graph construction must bypass the new "FanOut no outgoing edges"
    # topology rule? Actually the FanOut has no outgoing edges here — the
    # spec carries the target. So this passes _validate_topology.
    graph = Graph(
        id="g-fanin-upstream-failed",
        description="Begin -> FanOut(broadcast drain_then_fail) -> End",
        nodes=[
            _BeginNode(id="begin"),
            _FanOutNode(
                id="fan",
                specs=[
                    FanOutSpec(
                        kind="broadcast",
                        target_node_id="worker",
                        count=2,
                        on_failure="drain_then_fail",
                    ),
                ],
            ),
            _AgentNodeRef(
                id="worker",
                agent_id="ag",
                input_template="W{{ fanout_index }}",
            ),
            _EndNode(id="exit"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="fan"),
            _StaticEdge(from_node="worker", to_node="exit"),
        ],
        max_iterations=10,
    )

    # Worker[0] succeeds, worker[1] fails (LLM stream raises). The Done event
    # is needed so the success path closes cleanly.
    class _MixedLLM:
        async def list_models(self):
            return ["m"]

        def stream(self, *, model: str, messages: list[Message], **kwargs: Any):
            # Find prompt text to decide which worker we're handling.
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
            if "W1" in text:
                return self._fail(text)
            return self._ok(text)

        async def _ok(self, text: str):
            yield TextDelta(text=text, index=0)
            yield Done(stop_reason="stop", raw_reason="stop")

        async def _fail(self, text: str):
            if False:
                yield  # pragma: no cover
            raise RuntimeError(f"simulated worker failure for prompt={text!r}")

    repo = await _make_state_repo(tmp_path)

    async def agent_resolver(agent_id: str) -> Agent:
        return _agent(agent_id)

    llm = _MixedLLM()

    async def llm_resolver(_a: Agent):
        return (llm, _model())

    executor = _TestExecutor(
        graph=graph,
        agent_resolver=agent_resolver,
        llm_resolver=llm_resolver,  # type: ignore[arg-type]
        state_repo=repo,
        graph_session_id="gsid-fanin-upstream",
    )
    events = await _drain(executor.invoke([]))

    err = _terminal_error(events)
    assert err.code == "fanin_upstream_failed"

    rec = _translate_to_record(err)
    assert rec.kind == SessionMessageKind.ERROR
    assert rec.payload["code"] == "fanin_upstream_failed"

    state = await executor.load_state()
    assert state is not None
    assert state["ended_reason"] == "failed"
    assert state["ended_detail"] == "fanin_upstream_failed"
