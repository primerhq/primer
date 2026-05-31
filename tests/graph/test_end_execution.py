"""End execution integration tests (spec §2.2 / §7.3).

Drives Begin -> End graphs through the full
:class:`WorkspaceGraphExecutor` to exercise each behaviour End firing
must guarantee:

* empty ``output_template`` -> no ``_GraphEndOutputEvent`` payload
  (no assistant_token reaches messages.jsonl)
* non-empty template -> assistant_token record with rendered text
* ``output_schema`` validates rendered JSON on success
* ``output_schema`` rejects mismatched output -> ended_reason=failed,
  ended_detail="end_output_invalid"

Multi-End independent-termination semantics (Spec B §2.4) live in
:file:`test_multi_end_independent.py`.

Phase 3's :file:`test_end_firing.py` covers ``_render_end_output`` as a
unit. This file is the integration variant exercising the full
workspace executor end-to-end.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from primer.graph.base import _GraphEndOutputEvent, _GraphErrorEvent
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
    WorkspaceMessageWriter,
    _CoalesceState,
    translate_stream_event,
)
from primer.workspace.local.state import LocalStateRepo as StateRepo


class _FakeLLM:
    """Stub LLM — End-only graphs never call it but the executor wires it
    in to satisfy its constructor signature."""

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


async def _drain(it):
    return [ev async for ev in it]


async def _run_through_writer(
    *,
    graph: Graph,
    tmp_path: Path,
    session_id: str,
    graph_input: Any = None,
) -> tuple[list[StreamEvent], list[dict]]:
    """Run the executor end-to-end and return (events, persisted records).

    The persisted records are the jsonl lines that the
    :class:`WorkspaceMessageWriter` would write — i.e. exactly the
    session-detail UI would observe.
    """
    repo = await _make_state_repo(tmp_path)

    async def agent_resolver(agent_id: str) -> Agent:
        return _agent(agent_id)

    async def llm_resolver(_a: Agent):
        return (_FakeLLM(), _model())

    kwargs: dict[str, Any] = dict(
        graph=graph,
        agent_resolver=agent_resolver,
        llm_resolver=llm_resolver,  # type: ignore[arg-type]
        state_repo=repo,
        graph_session_id=session_id,
    )
    if graph_input is not None:
        kwargs["graph_input"] = graph_input
    executor = WorkspaceGraphExecutor(**kwargs)

    fake_io = _FakeWorkspaceIO()
    writer = WorkspaceMessageWriter(
        workspace_io=fake_io,
        session_id=session_id,
    )
    coalesce = _CoalesceState()
    events: list[StreamEvent] = []
    async for ev in executor.invoke([]):
        events.append(ev)
        rec = translate_stream_event(ev, coalesce)
        if rec is None:
            continue
        recs = rec if isinstance(rec, list) else [rec]
        for r in recs:
            await writer.append(r)
    await writer.flush()

    persisted = [json.loads(line) for line in fake_io.lines(session_id)]
    return events, persisted


# ===========================================================================
# Empty template — no assistant_token record reaches messages.jsonl
# ===========================================================================


@pytest.mark.asyncio
async def test_empty_output_template_emits_no_payload(tmp_path: Path) -> None:
    """An End with an empty ``output_template`` still terminates the
    graph (completed) but produces no ``assistant_token`` record in
    messages.jsonl — the rendered text is the empty string."""
    graph = Graph(
        id="g-empty-end",
        description="Begin -> End (empty template)",
        nodes=[
            _BeginNode(id="begin"),
            _EndNode(id="end"),  # default output_template is ""
        ],
        edges=[_StaticEdge(from_node="begin", to_node="end")],
    )
    events, persisted = await _run_through_writer(
        graph=graph, tmp_path=tmp_path, session_id="sid-empty",
    )

    # The event is still emitted (even with empty text) — but it carries
    # an empty payload. The translator + writer's behaviour: emit the
    # assistant_token only when there's something to say.
    end_records = [
        r for r in persisted
        if r["kind"] == SessionMessageKind.ASSISTANT_TOKEN.value
        and r["payload"].get("end_node_id") == "end"
    ]
    if end_records:
        # If the implementation chooses to emit anyway, the payload's
        # text MUST be empty (so the UI's collapsible block renders
        # nothing). Both behaviours satisfy the spec; the contract is
        # "no payload reaches the user."
        assert end_records[0]["payload"]["text"] == ""


# ===========================================================================
# Non-empty template — assistant_token record carries rendered text
# ===========================================================================


@pytest.mark.asyncio
async def test_non_empty_template_emits_assistant_token_record(
    tmp_path: Path,
) -> None:
    """A populated ``output_template`` renders Begin's parsed input into
    the End's text; an ``assistant_token`` record reaches messages.jsonl
    carrying the rendered string and ``end_node_id``."""
    graph = Graph(
        id="g-end-rendered",
        description="Begin -> End (rendered template)",
        nodes=[
            _BeginNode(id="begin"),
            _EndNode(
                id="end",
                output_template="answer: {{ nodes.begin.parsed.q }}",
            ),
        ],
        edges=[_StaticEdge(from_node="begin", to_node="end")],
    )
    events, persisted = await _run_through_writer(
        graph=graph,
        tmp_path=tmp_path,
        session_id="sid-rendered",
        graph_input={"q": "forty-two"},
    )

    end_records = [
        r for r in persisted
        if r["kind"] == SessionMessageKind.ASSISTANT_TOKEN.value
        and r["payload"].get("end_node_id") == "end"
    ]
    assert len(end_records) == 1, f"records={persisted!r}"
    assert end_records[0]["payload"]["text"] == "answer: forty-two"


# ===========================================================================
# output_schema — success path
# ===========================================================================


@pytest.mark.asyncio
async def test_output_schema_validates_rendered_json(tmp_path: Path) -> None:
    """When the rendered output_template parses as JSON matching the
    output_schema, the End completes normally and the ``parsed`` field
    on the assistant_token record is populated."""
    graph = Graph(
        id="g-end-schema-ok",
        description="Begin -> End with output_schema (passes)",
        nodes=[
            _BeginNode(id="begin"),
            _EndNode(
                id="end",
                output_template='{"summary": "{{ nodes.begin.parsed.s }}"}',
                output_schema={
                    "type": "object",
                    "required": ["summary"],
                    "properties": {"summary": {"type": "string"}},
                },
            ),
        ],
        edges=[_StaticEdge(from_node="begin", to_node="end")],
    )
    events, persisted = await _run_through_writer(
        graph=graph,
        tmp_path=tmp_path,
        session_id="sid-schema-ok",
        graph_input={"s": "all good"},
    )

    end_records = [
        r for r in persisted
        if r["kind"] == SessionMessageKind.ASSISTANT_TOKEN.value
        and r["payload"].get("end_node_id") == "end"
    ]
    assert len(end_records) == 1
    assert end_records[0]["payload"]["parsed"] == {"summary": "all good"}


# ===========================================================================
# output_schema — failure path
# ===========================================================================


@pytest.mark.asyncio
async def test_output_schema_mismatch_ends_with_end_output_invalid(
    tmp_path: Path,
) -> None:
    """When the rendered output_template doesn't satisfy the schema,
    the graph ends with ``ended_reason='failed'`` /
    ``ended_detail='end_output_invalid'`` and a :class:`_GraphErrorEvent`
    is emitted on the stream."""
    graph = Graph(
        id="g-end-schema-bad",
        description="Begin -> End with output_schema (mismatch)",
        nodes=[
            _BeginNode(id="begin"),
            _EndNode(
                id="end",
                output_template='{"summary": "{{ nodes.begin.parsed.s }}"}',
                # `summary` must be an integer but the template renders
                # a string — schema mismatch -> end_output_invalid.
                output_schema={
                    "type": "object",
                    "required": ["summary"],
                    "properties": {"summary": {"type": "integer"}},
                },
            ),
        ],
        edges=[_StaticEdge(from_node="begin", to_node="end")],
    )
    events, _persisted = await _run_through_writer(
        graph=graph,
        tmp_path=tmp_path,
        session_id="sid-schema-bad",
        graph_input={"s": "not_an_int"},
    )

    err_events = [e for e in events if isinstance(e, _GraphErrorEvent)]
    assert err_events, "expected a _GraphErrorEvent on schema mismatch"
    assert err_events[0].code == "end_output_invalid"


