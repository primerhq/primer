"""A subagent that fails keeps the text it had streamed, as its own record ahead of its error (ticket 01a11ca9; the delegated twin of PR 511).

``DelegationRecorder`` translated every subagent's events with ONE ``_CoalesceState``, and a state flushes its text buffer at ``Done`` and at a tool call
boundary, not at an ``Error``. So a subagent that streamed text and then failed wrote only its ERROR record; the text stayed in the shared buffer and was
MERGED into the next text of whatever run came next ("grandchild ...child ...", attributed to the child). Concurrent subagent runs shared the buffer the
same way. The recorder now keeps a coalescing state PER RUN, flushes a run's buffered output ahead of its fatal Error, and offers ``finish_run`` for the
runs that end some other way (an exception, a Stop), which the invoke loops call in a ``finally``.
"""

from __future__ import annotations

import asyncio

from primer.agent.call_scope import CallScope, bind_call_scope
from primer.model.chat import Done, Error, ReasoningDelta, TextDelta
from primer.session.delegation import DelegationRecorder


class _Writer:
    def __init__(self) -> None:
        self.records: list = []

    async def append(self, rec) -> int:
        self.records.append(rec)
        return len(self.records)


class _Bus:
    async def publish(self, key, payload) -> None:
        return None


class _CountingBus:
    def __init__(self) -> None:
        self.published: list = []

    async def publish(self, key, payload) -> None:
        self.published.append((key, payload))


def _run(n: int) -> dict:
    return {"delegate_tool_call_id": "call_0", "delegate_run_id": f"run-{n}", "delegate_parent_run_id": None, "delegate_depth": 1}


def _recorder() -> tuple[DelegationRecorder, _Writer]:
    writer = _Writer()
    return DelegationRecorder(writer=writer, event_bus=_Bus(), session_id="s"), writer


def _summary(writer: _Writer) -> list[tuple[str, str, str | None]]:
    """(kind, text or message, run id) per record, in log order."""
    return [
        (r.kind.value, r.payload.get("text") or r.payload.get("message") or "", r.payload.get("delegate_run_id"))
        for r in writer.records
    ]


async def test_a_runs_text_before_a_fatal_error_is_its_own_record_ahead_of_the_error() -> None:
    rec, w = _recorder()
    await rec.on_event(TextDelta(index=0, text="partial answer"), **_run(1))
    await rec.on_event(Error(message="the model fell over", code="server_error", fatal=True), **_run(1))

    assert _summary(w) == [("assistant_token", "partial answer", "run-1"), ("error", "the model fell over", "run-1")]
    assert all(r.payload["delegated"] is True and r.payload["delegate_tool_call_id"] == "call_0" for r in w.records)


async def test_the_next_run_is_not_prefixed_with_the_failed_runs_text() -> None:
    rec, w = _recorder()
    await rec.on_event(TextDelta(index=0, text="partial answer"), **_run(1))
    await rec.on_event(Error(message="the model fell over", code="server_error", fatal=True), **_run(1))
    await rec.on_event(TextDelta(index=0, text="second run"), **_run(2))
    await rec.on_event(Done(stop_reason="stop", raw_reason="stop"), **_run(2))

    tokens = [(r.payload["text"], r.payload["delegate_run_id"]) for r in w.records if r.kind.value == "assistant_token"]
    assert tokens == [("partial answer", "run-1"), ("second run", "run-2")], tokens


async def test_reasoning_goes_ahead_of_the_text_and_both_ahead_of_the_error() -> None:
    rec, w = _recorder()
    await rec.on_event(ReasoningDelta(index=0, text="thinking"), **_run(1))
    await rec.on_event(TextDelta(index=0, text="partial"), **_run(1))
    await rec.on_event(Error(message="boom", code="server_error", fatal=True), **_run(1))

    assert [kind for kind, _text, _run_id in _summary(w)] == ["reasoning", "assistant_token", "error"]


async def test_a_non_fatal_error_does_not_split_the_runs_text() -> None:
    """The stream goes on after it, so what came before and after is one answer (the main path flushes only for a fatal Error too)."""
    rec, w = _recorder()
    await rec.on_event(TextDelta(index=0, text="before "), **_run(1))
    await rec.on_event(Error(message="hiccup", code="x", fatal=False), **_run(1))
    await rec.on_event(TextDelta(index=0, text="after"), **_run(1))
    await rec.on_event(Done(stop_reason="stop", raw_reason="stop"), **_run(1))

    tokens = [r.payload["text"] for r in w.records if r.kind.value == "assistant_token"]
    assert tokens == ["before after"], tokens


async def test_two_runs_streaming_at_once_keep_their_text_apart() -> None:
    rec, w = _recorder()
    await rec.on_event(TextDelta(index=0, text="a1 "), **_run(1))
    await rec.on_event(TextDelta(index=0, text="b1 "), **_run(2))
    await rec.on_event(TextDelta(index=0, text="a2"), **_run(1))
    await rec.on_event(Done(stop_reason="stop", raw_reason="stop"), **_run(1))
    await rec.on_event(TextDelta(index=0, text="b2"), **_run(2))
    await rec.on_event(Done(stop_reason="stop", raw_reason="stop"), **_run(2))

    tokens = sorted((r.payload["delegate_run_id"], r.payload["text"]) for r in w.records if r.kind.value == "assistant_token")
    assert tokens == [("run-1", "a1 a2"), ("run-2", "b1 b2")], tokens


async def test_finish_run_writes_what_a_run_streamed_when_it_ended_without_a_done() -> None:
    """The run raised, or was stopped, after streaming text: no Done and no Error event ever reached the recorder."""
    rec, w = _recorder()
    await rec.on_event(TextDelta(index=0, text="half an answer"), **_run(1))
    await rec.finish_run(**_run(1))
    await rec.on_event(TextDelta(index=0, text="next"), **_run(2))
    await rec.on_event(Done(stop_reason="stop", raw_reason="stop"), **_run(2))

    tokens = [(r.payload["text"], r.payload["delegate_run_id"]) for r in w.records if r.kind.value == "assistant_token"]
    assert tokens == [("half an answer", "run-1"), ("next", "run-2")], tokens


async def test_finish_run_with_nothing_buffered_or_for_an_unknown_run_writes_nothing() -> None:
    rec, w = _recorder()
    await rec.on_event(TextDelta(index=0, text="done answer"), **_run(1))
    await rec.on_event(Done(stop_reason="stop", raw_reason="stop"), **_run(1))
    before = len(w.records)
    await rec.finish_run(**_run(1))
    await rec.finish_run(**_run(9))
    assert len(w.records) == before
    assert rec._states == {}, "a finished run's coalescing state is forgotten"


async def test_finish_run_forgets_the_state_of_a_run_that_had_text_to_flush() -> None:
    rec, w = _recorder()
    await rec.on_event(TextDelta(index=0, text="half an answer"), **_run(1))
    await rec.finish_run(**_run(1))
    assert rec._states == {} and len(w.records) == 1


async def test_a_run_that_ends_by_a_cancellation_is_forgotten_and_nothing_is_written_or_ticked() -> None:
    """A hard cancel the turn must not land (a lost lease, an unflagged cancel, a force-deleted row) leaves the log alone: dispatch's cancelled exit decides what
    partial output is the turn's to land. finish_run(flush=False) only forgets the run."""
    writer, bus = _Writer(), _CountingBus()
    rec = DelegationRecorder(writer=writer, event_bus=bus, session_id="s")
    await rec.on_event(TextDelta(index=0, text="half an answer"), **_run(1))
    await rec.finish_run(flush=False, **_run(1))
    assert writer.records == [] and bus.published == []
    assert rec._states == {}


async def test_the_state_is_created_once_per_run_not_once_per_event() -> None:
    rec, _w = _recorder()
    await rec.on_event(TextDelta(index=0, text="a"), **_run(1))
    first = rec._states["run-1"]
    await rec.on_event(TextDelta(index=0, text="b"), **_run(1))
    assert rec._states["run-1"] is first


async def test_finish_run_writes_nothing_once_the_call_was_abandoned() -> None:
    """Stop slice B1: nothing a given-up call emits may land after the Stop's answer to it."""
    rec, w = _recorder()
    scope = CallScope()

    async def work():
        bind_call_scope(scope)                      # as a tool call runs: in its own task, with the scope bound in that task's context
        await rec.on_event(TextDelta(index=0, text="half an answer"), **_run(1))
        scope.abandon()
        await rec.finish_run(**_run(1))

    await asyncio.create_task(work())
    assert w.records == [], "an abandoned call kept appending to the parent log"
