"""When the inner executor yields a ``_GraphErrorEvent``, the
session-translator (``primer.session.persistence.translate_stream_event``)
produces a ``SessionMessageRecord(kind=ERROR, payload={code, node_id, ...})``.

This couples the graph runtime's spec §5.4 terminal-error event to the
session persistence layer's error record so the session detail page's WS
replay surfaces graph failures the same way as any other turn failure.

Two pieces are tested together:

1. The translator emits the expected record for a synthetic
   ``_GraphErrorEvent``.
2. A real Begin→End graph with a bad ``output_schema`` (rendered output
   isn't valid JSON for the schema) yields a ``_GraphErrorEvent`` whose
   translated record carries ``payload.code == 'end_output_invalid'``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from primer.graph.base import _GraphErrorEvent
from primer.graph.workspace_executor import WorkspaceGraphExecutor
from primer.model.agent import Agent, AgentModel
from primer.model.chat import StreamEvent
from primer.model.graph import (
    Graph,
    _BeginNode,
    _EndNode,
    _StaticEdge,
)
from primer.model.provider import LLMModel
from primer.model.workspace_session import SessionMessageKind
from primer.session.persistence import _CoalesceState, translate_stream_event
from primer.workspace.local.state import LocalStateRepo as StateRepo


class _FakeLLM:
    async def list_models(self):
        return ["m"]

    def stream(self, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        async def _empty() -> AsyncIterator[StreamEvent]:
            if False:
                yield  # pragma: no cover
        return _empty()


async def _make_state_repo(tmp_path: Path) -> StateRepo:
    repo = StateRepo(tmp_path / ".state", workspace_id="ws-test")
    await repo.initialize()
    return repo


def _model() -> LLMModel:
    return LLMModel(name="m", context_length=128_000)


def _agent(agent_id: str) -> Agent:
    return Agent(
        id=agent_id,
        description=f"agent {agent_id}",
        model=AgentModel(provider_id="p", model_name="m"),
    )


async def _drain(it: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    return [ev async for ev in it]


def test_translate_graph_error_event_to_error_record() -> None:
    """Synthetic ``_GraphErrorEvent`` translates to ``SessionMessageKind.ERROR``."""
    ev = _GraphErrorEvent(
        code="end_output_invalid",
        message="output is not JSON",
        node_id="end",
        path=None,
    )
    rec = translate_stream_event(ev, _CoalesceState())  # type: ignore[arg-type]
    assert rec is not None
    assert not isinstance(rec, list)
    assert rec.kind == SessionMessageKind.ERROR
    assert rec.payload["code"] == "end_output_invalid"
    assert rec.payload["node_id"] == "end"
    assert "output is not JSON" in rec.payload["message"]
    assert rec.payload["path"] is None


def test_translate_routing_failed_event() -> None:
    """Routing-failed code carries ``node_id`` of the source node."""
    ev = _GraphErrorEvent(
        code="routing_failed",
        message="no branch matched",
        node_id="branch_node",
    )
    rec = translate_stream_event(ev, _CoalesceState())  # type: ignore[arg-type]
    assert rec is not None
    assert not isinstance(rec, list)
    assert rec.kind == SessionMessageKind.ERROR
    assert rec.payload["code"] == "routing_failed"
    assert rec.payload["node_id"] == "branch_node"


@pytest.mark.asyncio
async def test_end_output_invalid_emits_graph_error_event(
    tmp_path: Path,
) -> None:
    """End with a bad output_schema yields a ``_GraphErrorEvent`` whose
    translated record carries ``payload.code == 'end_output_invalid'``."""
    graph = Graph(
        id="g-end-invalid",
        description="Begin -> End with non-JSON output and a schema",
        entry_node_id="begin",
        nodes=[
            _BeginNode(id="begin"),
            _EndNode(
                id="end",
                # Non-JSON text but schema demands an object — End will
                # fail with end_output_invalid.
                output_template="just plain text not JSON",
                output_schema={"type": "object"},
            ),
        ],
        edges=[_StaticEdge(from_node="begin", to_node="end")],
    )
    repo = await _make_state_repo(tmp_path)

    async def agent_resolver(agent_id: str) -> Agent:
        return _agent(agent_id)

    async def llm_resolver(_a: Agent):
        return (_FakeLLM(), _model())

    executor = WorkspaceGraphExecutor(
        graph=graph,
        agent_resolver=agent_resolver,
        llm_resolver=llm_resolver,  # type: ignore[arg-type]
        state_repo=repo,
        graph_session_id="gsid-err",
        graph_input={"q": "x"},
    )
    events = await _drain(executor.invoke([]))

    # Find the terminal _GraphErrorEvent emitted by the base executor.
    err_events = [e for e in events if isinstance(e, _GraphErrorEvent)]
    assert len(err_events) == 1, (
        f"expected one _GraphErrorEvent, got events={events!r}"
    )
    err = err_events[0]
    assert err.code == "end_output_invalid"
    assert err.node_id == "end"

    # The session-layer translator turns that into an ERROR record.
    state = _CoalesceState()
    rec = translate_stream_event(err, state)  # type: ignore[arg-type]
    assert rec is not None
    assert not isinstance(rec, list)
    assert rec.kind == SessionMessageKind.ERROR
    assert rec.payload["code"] == "end_output_invalid"
    assert rec.payload["node_id"] == "end"

    # State is ENDED/failed with the detail propagated.
    persisted = await executor.load_state()
    assert persisted is not None
    assert persisted["status"] == "ended"
    assert persisted["ended_reason"] == "failed"
    assert persisted["ended_detail"] == "end_output_invalid"
