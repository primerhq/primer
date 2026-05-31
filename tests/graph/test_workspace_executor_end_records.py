"""End firing emits an ``assistant_token`` ``SessionMessageRecord``.

Spec §4.4 / §2.2. The base executor emits a ``_GraphEndOutputEvent``
immediately after End fires; the session-layer translator
(``primer.session.persistence.translate_stream_event``) converts it
into a ``SessionMessageRecord(kind=assistant_token, payload={text,
parsed, end_node_id})``. The terminal ``done`` record continues to come
from the session-dispatch post-turn path (``primer/session/dispatch.py``
Section 6 — DONE record on clean completion).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from primer.graph.base import _GraphEndOutputEvent
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
from primer.session.persistence import (
    WorkspaceIO,
    WorkspaceMessageWriter,
    _CoalesceState,
    translate_stream_event,
)
from primer.workspace.local.state import LocalStateRepo as StateRepo


class _FakeLLM:
    async def list_models(self):
        return ["m"]

    def stream(self, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        async def _empty() -> AsyncIterator[StreamEvent]:
            if False:
                yield  # pragma: no cover
        return _empty()


class _FakeWorkspaceIO:
    """In-memory ``WorkspaceIO`` capturing every appended jsonl line."""

    def __init__(self) -> None:
        self.buffers: dict[str, bytearray] = {}

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        self.buffers.setdefault(session_id, bytearray()).extend(line)

    def lines(self, session_id: str) -> list[str]:
        raw = bytes(self.buffers.get(session_id, b""))
        return [s for s in raw.decode("utf-8").split("\n") if s]


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


def test_translator_emits_assistant_token_for_end_output_event() -> None:
    """Synthetic ``_GraphEndOutputEvent`` translates to an
    ``assistant_token`` record with ``text`` / ``parsed`` / ``end_node_id``."""
    ev = _GraphEndOutputEvent(
        text="final answer",
        parsed={"summary": "ok"},
        end_node_id="end",
    )
    rec = translate_stream_event(ev, _CoalesceState())  # type: ignore[arg-type]
    assert rec is not None
    assert not isinstance(rec, list)
    assert rec.kind == SessionMessageKind.ASSISTANT_TOKEN
    assert rec.payload == {
        "text": "final answer",
        "parsed": {"summary": "ok"},
        "end_node_id": "end",
    }


def test_translator_emits_assistant_token_with_parsed_none() -> None:
    """Plain-text End output renders an assistant_token with parsed=None."""
    ev = _GraphEndOutputEvent(
        text="plain",
        parsed=None,
        end_node_id="end",
    )
    rec = translate_stream_event(ev, _CoalesceState())  # type: ignore[arg-type]
    assert rec is not None
    assert not isinstance(rec, list)
    assert rec.kind == SessionMessageKind.ASSISTANT_TOKEN
    assert rec.payload["text"] == "plain"
    assert rec.payload["parsed"] is None
    assert rec.payload["end_node_id"] == "end"


@pytest.mark.asyncio
async def test_end_firing_emits_event_in_stream(tmp_path: Path) -> None:
    """A real Begin→End run emits exactly one ``_GraphEndOutputEvent``
    carrying the rendered text + the End node's id."""
    graph = Graph(
        id="g-end-emits",
        description="Begin -> End emits assistant_token event",
        nodes=[
            _BeginNode(id="begin"),
            _EndNode(
                id="end",
                output_template="hello {{ nodes.begin.parsed.who }}",
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
        graph_session_id="gsid-end-emit",
        graph_input={"who": "world"},
    )
    events = await _drain(executor.invoke([]))

    end_events = [e for e in events if isinstance(e, _GraphEndOutputEvent)]
    assert len(end_events) == 1, (
        f"expected one _GraphEndOutputEvent, got events={events!r}"
    )
    end = end_events[0]
    assert end.text == "hello world"
    assert end.parsed is None
    assert end.end_node_id == "end"


@pytest.mark.asyncio
async def test_end_firing_writes_assistant_token_to_messages_jsonl(
    tmp_path: Path,
) -> None:
    """End firing's event, when translated and persisted through
    ``WorkspaceMessageWriter``, lands in ``messages.jsonl`` as an
    ``assistant_token`` record carrying ``text``, ``parsed`` and
    ``end_node_id``."""
    graph = Graph(
        id="g-end-jsonl",
        description="Begin -> End, persisted via the session writer",
        nodes=[
            _BeginNode(id="begin"),
            _EndNode(
                id="end",
                output_template="hello {{ nodes.begin.parsed.who }}",
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
        graph_session_id="gsid-end-jsonl",
        graph_input={"who": "matrix"},
    )

    fake_io = _FakeWorkspaceIO()
    writer = WorkspaceMessageWriter(
        workspace_io=fake_io,
        session_id="gsid-end-jsonl",
    )
    coalesce = _CoalesceState()
    async for ev in executor.invoke([]):
        rec = translate_stream_event(ev, coalesce)
        if rec is None:
            continue
        recs = rec if isinstance(rec, list) else [rec]
        for r in recs:
            await writer.append(r)
    await writer.flush()

    # Re-parse the captured jsonl lines.
    import json
    lines = fake_io.lines("gsid-end-jsonl")
    assert lines, "expected at least one record written"
    records = [json.loads(line) for line in lines]

    end_records = [
        r for r in records
        if r["kind"] == SessionMessageKind.ASSISTANT_TOKEN.value
        and r["payload"].get("end_node_id") == "end"
    ]
    assert len(end_records) == 1, (
        f"expected one assistant_token end record, got records={records!r}"
    )
    end_rec = end_records[0]
    assert end_rec["payload"]["text"] == "hello matrix"
    assert end_rec["payload"]["parsed"] is None
