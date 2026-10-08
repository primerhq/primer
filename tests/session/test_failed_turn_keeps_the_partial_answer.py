"""A model that dies mid-answer keeps the half answer the user watched stream in (found 2026-10-08 while verifying PR 480).

Text and reasoning coalesce in ``_CoalesceState`` and only become records at a tool call or at Done. A user Cancel or Stop flushed them
(``flush_partial_output``, in ``run_one_session_turn``'s finally), but a FAILED turn never did: after a refresh the transcript showed the
user's message and the error, and the answer that had been streaming was gone. The durable log of a scripted run was
``user_input, llm_call, error, error, error`` with no ``assistant_token``.

Two failure shapes reach the log, and both are pinned:

* the stream's own fatal ``Error`` event (``translate_stream_event``), which the agent loop then raises as ``TurnStreamFailure``: the
  partial text must land BEFORE that Error record, as it does before a ``cancelled`` one;
* an exception raised out of the stream (a timeout, a bug): ``_end_turn_failed`` flushes before it writes its own ERROR record.

A lost lease is deliberately not one of them: the session may belong to another worker now, and this execution writes nothing on its way out.
"""

from __future__ import annotations

import json
import logging

import pytest

import primer.session.dispatch as dispatch
from primer.model.chat import Error, ReasoningDelta, TextDelta, ToolCallEnd, ToolCallStart, TurnStreamFailure
from primer.model.workspace_session import SessionMessageKind, SessionStatus, WorkspaceSession
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn
from primer.session.persistence import _CoalesceState, translate_stream_event
from tests.session.test_dispatch import (  # noqa: F401  (fixtures are used by name)
    FakeExecutor,
    FakeWorkspaceIO,
    _make_lease,
    fake_event_bus,
    fake_storage_provider,
    fake_workspace_io,
    seeded_session,
)


async def _run(seeded_session, io, bus, storage, events):
    async def build(_session: WorkspaceSession):
        return FakeExecutor(events)

    deps = SessionDispatchDeps(storage_provider=storage, workspace_io=io, event_bus=bus, build_executor=build)
    return await run_one_session_turn(_make_lease(seeded_session.id), deps)


def _records(io: FakeWorkspaceIO, session_id: str) -> list[dict]:
    return [json.loads(line) for line in io.read_lines(session_id)]


def _shape(records: list[dict]) -> list[str]:
    keep = {SessionMessageKind.REASONING, SessionMessageKind.ASSISTANT_TOKEN, SessionMessageKind.ERROR, SessionMessageKind.TOOL_CALL}
    return [r["kind"] for r in records if r["kind"] in keep]


def _failure() -> TurnStreamFailure:
    return TurnStreamFailure(Error(code="server_error", message="boom", fatal=True), partial_messages=[], rounds_completed=0)


@pytest.mark.asyncio
async def test_a_stream_error_keeps_the_text_that_streamed_before_it(
    seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
) -> None:
    """The model dies mid-answer: the stream's fatal Error event, then the loop's TurnStreamFailure (as in the agent loop)."""
    events = [
        TextDelta(text="par", index=0), TextDelta(text="tial", index=0),
        Error(code="server_error", message="boom", fatal=True),
        _failure(),
    ]
    outcome = await _run(seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, events)

    assert outcome.success is False
    records = _records(fake_workspace_io, seeded_session.id)
    assert _shape(records) == [SessionMessageKind.ASSISTANT_TOKEN, SessionMessageKind.ERROR, SessionMessageKind.ERROR], (
        "the partial answer lands before the stream's own Error record, which precedes dispatch's"
    )
    token = next(r for r in records if r["kind"] == SessionMessageKind.ASSISTANT_TOKEN)
    assert token["payload"]["text"] == "partial"
    row = await fake_storage_provider.get_storage(WorkspaceSession).get(seeded_session.id)
    assert row.status == SessionStatus.ENDED and row.ended_reason == "failed" and row.ended_detail == "server_error"


@pytest.mark.asyncio
async def test_an_exception_out_of_the_stream_keeps_the_text_that_streamed_before_it(
    seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
) -> None:
    """A timeout or a bug raises out of the stream with no Error event at all."""
    events = [TextDelta(text="par", index=0), TextDelta(text="tial", index=0), RuntimeError("the model call blew up")]
    outcome = await _run(seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, events)

    assert outcome.success is False
    records = _records(fake_workspace_io, seeded_session.id)
    assert _shape(records) == [SessionMessageKind.ASSISTANT_TOKEN, SessionMessageKind.ERROR], "the text first, then the ERROR record"
    assert next(r for r in records if r["kind"] == SessionMessageKind.ASSISTANT_TOKEN)["payload"]["text"] == "partial"


@pytest.mark.asyncio
async def test_reasoning_comes_before_the_answer_in_a_failed_turn_too(
    seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
) -> None:
    events = [ReasoningDelta(text="thinking", index=0), TextDelta(text="answer", index=0), RuntimeError("boom")]
    await _run(seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, events)

    assert _shape(_records(fake_workspace_io, seeded_session.id)) == [
        SessionMessageKind.REASONING, SessionMessageKind.ASSISTANT_TOKEN, SessionMessageKind.ERROR,
    ]


@pytest.mark.asyncio
async def test_a_failure_with_nothing_streamed_writes_only_the_error(
    seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
) -> None:
    await _run(seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, [RuntimeError("boom")])
    assert _shape(_records(fake_workspace_io, seeded_session.id)) == [SessionMessageKind.ERROR], "no empty answer record"


@pytest.mark.asyncio
async def test_text_already_flushed_at_a_tool_call_is_not_written_twice(
    seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
) -> None:
    events = [
        TextDelta(text="first round", index=0),
        ToolCallStart(id="t1", name="x", index=0), ToolCallEnd(id="t1", arguments={}, index=0),
        TextDelta(text="second round", index=1),
        RuntimeError("boom"),
    ]
    await _run(seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, events)

    records = _records(fake_workspace_io, seeded_session.id)
    assert [r["payload"]["text"] for r in records if r["kind"] == SessionMessageKind.ASSISTANT_TOKEN] == ["first round", "second round"]
    assert _shape(records)[-2:] == [SessionMessageKind.ASSISTANT_TOKEN, SessionMessageKind.ERROR]


@pytest.mark.asyncio
async def test_a_flush_that_fails_never_stops_the_failure_from_landing(
    seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch, caplog,
) -> None:
    """The partial answer is best effort: whatever happens to it, the turn still ends failed with its ERROR record."""
    def explode(*_args, **_kwargs):
        raise OSError("workspace gone")

    monkeypatch.setattr(dispatch, "flush_partial_output", explode)
    with caplog.at_level(logging.WARNING):
        outcome = await _run(
            seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
            [TextDelta(text="partial", index=0), RuntimeError("boom")],
        )

    assert outcome.success is False
    assert SessionMessageKind.ERROR in _shape(_records(fake_workspace_io, seeded_session.id))
    row = await fake_storage_provider.get_storage(WorkspaceSession).get(seeded_session.id)
    assert row.status == SessionStatus.ENDED and row.ended_reason == "failed"
    assert "output streamed before the failure" in " | ".join(r.getMessage() for r in caplog.records)


# --- the translation of the stream's own Error event ---------------------------------------------------------------------------


def _buffered(text: str = "partial") -> _CoalesceState:
    state = _CoalesceState()
    assert translate_stream_event(TextDelta(text=text, index=0), state, turn_no=2) is None
    return state


def test_a_fatal_error_event_flushes_what_was_buffered_ahead_of_its_own_record() -> None:
    state = _buffered()
    out = translate_stream_event(Error(code="server_error", message="boom", fatal=True), state, turn_no=2)
    assert isinstance(out, list)
    assert [r.kind for r in out] == [SessionMessageKind.ASSISTANT_TOKEN, SessionMessageKind.ERROR]
    assert out[0].payload["text"] == "partial" and out[1].payload["code"] == "server_error"
    assert not state.text_buffers, "the buffer is drained, so nothing is written twice"


def test_a_fatal_error_event_with_nothing_buffered_is_still_a_single_record() -> None:
    out = translate_stream_event(Error(code="x", message="boom", fatal=True), _CoalesceState(), turn_no=0)
    assert not isinstance(out, list) and out.kind == SessionMessageKind.ERROR


def test_a_non_fatal_error_event_leaves_the_buffers_alone() -> None:
    """A recoverable error says more events follow; the answer is still being built."""
    state = _buffered()
    out = translate_stream_event(Error(code="x", message="retrying", fatal=False), state, turn_no=2)
    assert not isinstance(out, list) and out.kind == SessionMessageKind.ERROR
    assert state.text_buffers, "the partial text is still buffered"


def test_a_graph_runtime_error_flushes_every_node_ahead_of_its_own_record() -> None:
    from primer.graph.base import _GraphErrorEvent

    state = _CoalesceState()
    state.text_buffers["node-a"] = "from a"
    state.text_buffers["node-b"] = "from b"
    out = translate_stream_event(_GraphErrorEvent(code="node_failed", message="boom", node_id="node-b"), state, turn_no=1)
    assert isinstance(out, list)
    assert [r.kind for r in out] == [SessionMessageKind.ASSISTANT_TOKEN, SessionMessageKind.ASSISTANT_TOKEN, SessionMessageKind.ERROR]
    assert sorted(r.payload["text"] for r in out[:2]) == ["from a", "from b"]
