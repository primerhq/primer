"""Tests for the ephemeral delta stream (Phase 1).

Covers the load-bearing contracts that the spec pins:

* :class:`primer.tap.delta.DeltaBuffer` - per-part_id accumulation, 100 ms
  batch, ``part_start`` / ``*_delta`` / ``part_end`` framing, oversized
  payload split at the ~7600-byte safe cap, and swallow-on-publish-failure
  (a degraded bus must never error the turn).
* :func:`primer.session.persistence.translate_stream_event` - the live path
  feeds the sink for TextDelta / ReasoningDelta / ToolCallDelta while the
  coalescing is preserved, the durable records carry the matching
  ``part_id``, and the parts are closed at the durable record.
* :class:`primer.tap.router.WorkspaceTapRouter` - the ``session:{sid}:delta``
  channel fans out to ``subscribe_delta`` on its own (separate from the
  durable tick pointer, which stays the only cursor advance).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from primer.bus.in_memory import InMemoryEventBus
from primer.model.chat import (
    Done,
    ReasoningDelta,
    TextDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
)
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionMessageKind,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.persistence import _CoalesceState, translate_stream_event
from primer.tap.delta import DELTA_EVENT_SUFFIX, DeltaBuffer, part_id
from primer.tap.router import WorkspaceTapRouter


_TIMEOUT = 2.0


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _RecordingPublish:
    """An EventBus.publish that records (event_key, payload) and never raises."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, event_key: str, payload: dict) -> None:
        self.calls.append((event_key, payload))


class _FailingPublish:
    """An EventBus.publish that always raises (a degraded bus)."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, event_key: str, payload: dict) -> None:
        self.calls += 1
        raise RuntimeError("bus down")


class _RecordingSink:
    """A duck-typed delta sink that records on_delta / close calls."""

    def __init__(self) -> None:
        self.delta_calls: list[tuple[str, str, str]] = []
        self.close_calls: list[str] = []

    def on_delta(self, pid: str, kind: str, delta: str) -> None:
        self.delta_calls.append((pid, kind, delta))

    def close(self, pid: str) -> None:
        self.close_calls.append(pid)


def _session(sid: str, wid: str) -> WorkspaceSession:
    return WorkspaceSession(
        id=sid,
        workspace_id=wid,
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.RUNNING,
        created_at=_now(),
    )


async def _seed(storage_provider, session: WorkspaceSession) -> None:
    await storage_provider.get_storage(WorkspaceSession).create(session)


# ---------------------------------------------------------------------------
# DeltaBuffer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_buffer_part_lifecycle_emits_start_delta_end() -> None:
    calls = _RecordingPublish()
    buf = DeltaBuffer(session_id="s1", publish=calls)

    buf.on_delta(part_id(None, "text", 0), "text", "Hel")
    buf.on_delta(part_id(None, "text", 0), "text", "lo")
    await buf.flush()
    buf.close(part_id(None, "text", 0))
    await buf.aclose()

    classes = [c[1]["class"] for c in calls.calls]
    assert classes == ["part_start", "text_delta", "part_end"]
    # The deltas were batched into one frame.
    assert calls.calls[1][1]["delta"] == "Hello"
    # Every frame carries the part_id; the channel is the delta channel.
    assert all(c[1]["part_id"] == "x:text:0" for c in calls.calls)
    assert all(c[0].endswith(DELTA_EVENT_SUFFIX) for c in calls.calls)
    # No seq field -> the frame never advances the tap cursor.
    assert "seq" not in calls.calls[0][1]


@pytest.mark.asyncio
async def test_buffer_batches_multiple_deltas_into_one_frame() -> None:
    calls = _RecordingPublish()
    buf = DeltaBuffer(session_id="s1", publish=calls)
    for chunk in ("a", "b", "c", "d"):
        buf.on_delta("x:text", "text", chunk)
    await buf.aclose()
    text_deltas = [c[1] for c in calls.calls if c[1]["class"] == "text_delta"]
    assert len(text_deltas) == 1
    assert text_deltas[0]["delta"] == "abcd"


@pytest.mark.asyncio
async def test_buffer_splits_oversized_delta_at_safe_cap() -> None:
    calls = _RecordingPublish()
    buf = DeltaBuffer(session_id="s1", publish=calls)
    buf.on_delta("x:text", "text", "a" * 8000)
    buf.close("x:text")
    await buf.aclose()

    text_deltas = [c[1] for c in calls.calls if c[1]["class"] == "text_delta"]
    # 8000 bytes of "a" (1 byte each) must split into >= 2 frames.
    assert len(text_deltas) >= 2
    # Each chunk is within the 7600-byte safe cap.
    for frame in text_deltas:
        assert len(frame["delta"].encode("utf-8")) <= 7600
    # Reassembled content is intact.
    assert "".join(f["delta"] for f in text_deltas) == "a" * 8000


@pytest.mark.asyncio
async def test_buffer_swallows_publish_failure() -> None:
    calls = _FailingPublish()
    buf = DeltaBuffer(session_id="s1", publish=calls)
    buf.on_delta("x:text", "text", "hi")
    buf.close("x:text")
    # Must not raise even though the bus is down: a degraded bus degrades
    # to final-record behaviour (the durable record still completes the part).
    await buf.aclose()
    assert calls.calls >= 1


@pytest.mark.asyncio
async def test_buffer_ignors_delta_after_close() -> None:
    calls = _RecordingPublish()
    buf = DeltaBuffer(session_id="s1", publish=calls)
    buf.on_delta("x:text", "text", "a")
    buf.close("x:text")
    buf.on_delta("x:text", "text", "b")  # no-op: part is closed
    await buf.aclose()

    deltas = [c[1]["delta"] for c in calls.calls if c[1]["class"] == "text_delta"]
    assert deltas == ["a"]


@pytest.mark.asyncio
async def test_buffer_tool_part_uses_call_id() -> None:
    calls = _RecordingPublish()
    buf = DeltaBuffer(session_id="s1", publish=calls)
    # Tool input part_id is the tool call id (not node-scoped).
    buf.on_delta("call_0", "tool", '{"q":')
    buf.on_delta("call_0", "tool", '"x"}')
    buf.close("call_0")
    await buf.aclose()

    assert calls.calls[0][1]["part_id"] == "call_0"
    assert calls.calls[1][1]["class"] == "tool_input_delta"
    assert calls.calls[1][1]["kind"] == "tool"


@pytest.mark.asyncio
async def test_buffer_aclose_is_idempotent() -> None:
    calls = _RecordingPublish()
    buf = DeltaBuffer(session_id="s1", publish=calls)
    buf.on_delta("x:text", "text", "hi")
    buf.close("x:text")
    await buf.aclose()
    n = len(calls.calls)
    await buf.aclose()  # no second emission
    assert len(calls.calls) == n


# ---------------------------------------------------------------------------
# translate_stream_event integration
# ---------------------------------------------------------------------------


def test_translate_feeds_sink_and_stamps_part_id() -> None:
    state = _CoalesceState()
    sink = _RecordingSink()

    translate_stream_event(TextDelta(text="hi", index=0), state, delta_sink=sink)
    translate_stream_event(
        ReasoningDelta(text="think", index=0), state, delta_sink=sink
    )
    translate_stream_event(
        ToolCallStart(id="c1", name="search", index=0), state, delta_sink=sink
    )
    translate_stream_event(
        ToolCallDelta(id="c1", arguments_delta='{"q":', index=0),
        state,
        delta_sink=sink,
    )
    records = translate_stream_event(
        ToolCallEnd(id="c1", arguments={"q": "x"}, index=0),
        state,
        delta_sink=sink,
    )
    if not isinstance(records, list):
        records = [records]

    # 01a0518f: the tool part_id is the SCOPED id minted at ToolCallStart
    # ("x:tool:0:1" - node "x"/None, kind "tool", turn 0, seq 1), not the
    # raw provider id "c1" verbatim - it restarts at the same value every
    # llm.stream() call and can collide across tool-rounds / concurrent
    # fan-out siblings.
    scoped_tool_id = "x:tool:0:1"

    # The live path saw every content delta.
    assert ("x:text:0", "text", "hi") in sink.delta_calls
    assert ("x:reasoning:0", "reasoning", "think") in sink.delta_calls
    assert (scoped_tool_id, "tool", '{"q":') in sink.delta_calls
    # The parts are closed at the durable record.
    assert "x:text:0" in sink.close_calls
    assert "x:reasoning:0" in sink.close_calls
    assert scoped_tool_id in sink.close_calls

    # The durable records carry the matching part_id.
    by_kind = {r.kind: r for r in records}
    assert by_kind[SessionMessageKind.ASSISTANT_TOKEN].payload["part_id"] == "x:text:0"
    assert by_kind[SessionMessageKind.REASONING].payload["part_id"] == "x:reasoning:0"
    # The tool input part reconciles to the TOOL_CALL record by its
    # (now scoped) id - live and durable still agree.
    assert by_kind[SessionMessageKind.TOOL_CALL].payload["id"] == scoped_tool_id


def test_translate_without_sink_is_unchanged() -> None:
    # No sink -> the live path is skipped; records are produced as before,
    # still carrying part_id (the durable record always carries it).
    state = _CoalesceState()
    translate_stream_event(TextDelta(text="hi", index=0), state)
    records = translate_stream_event(
        ToolCallEnd(id="c1", arguments={"q": "x"}, index=0), state
    )
    if not isinstance(records, list):
        records = [records]
    by_kind = {r.kind: r for r in records}
    assert by_kind[SessionMessageKind.ASSISTANT_TOKEN].payload["part_id"] == "x:text:0"
    assert by_kind[SessionMessageKind.TOOL_CALL].payload["id"] == "c1"


def test_translate_done_closes_parts_and_closes_reasoning_first() -> None:
    state = _CoalesceState()
    sink = _RecordingSink()
    translate_stream_event(
        ReasoningDelta(text="think", index=0), state, delta_sink=sink
    )
    translate_stream_event(TextDelta(text="hi", index=0), state, delta_sink=sink)
    records = translate_stream_event(
        Done(stop_reason="stop", raw_reason="stop"), state, delta_sink=sink
    )
    if not isinstance(records, list):
        records = [records]
    # Reasoning precedes the answer text in the flushed records.
    assert records[0].kind == SessionMessageKind.REASONING
    assert records[1].kind == SessionMessageKind.ASSISTANT_TOKEN
    assert "x:text:0" in sink.close_calls
    assert "x:reasoning:0" in sink.close_calls


# ---------------------------------------------------------------------------
# Router fan-out of the delta channel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_router_fans_delta_to_subscribe_delta(
    fake_storage_provider,
) -> None:
    bus = InMemoryEventBus()
    await bus.initialize()
    await _seed(fake_storage_provider, _session("s1", "W"))
    router = WorkspaceTapRouter(
        bus, fake_storage_provider.get_storage(WorkspaceSession)
    )
    await router.start()
    try:
        sub = router.subscribe_delta("W")
        try:
            await bus.publish(
                f"session:s1{DELTA_EVENT_SUFFIX}",
                {
                    "class": "text_delta",
                    "session_id": "s1",
                    "part_id": "x:text",
                    "kind": "text",
                    "delta": "hi",
                },
            )
            frame = await asyncio.wait_for(sub.__anext__(), timeout=_TIMEOUT)
            assert frame["class"] == "text_delta"
            assert frame["part_id"] == "x:text"
            assert frame["delta"] == "hi"
        finally:
            await sub.aclose()
    finally:
        await router.aclose()
        await bus.aclose()


@pytest.mark.asyncio
async def test_router_delta_does_not_reach_tick_subscriber(
    fake_storage_provider,
) -> None:
    bus = InMemoryEventBus()
    await bus.initialize()
    await _seed(fake_storage_provider, _session("s1", "W"))
    router = WorkspaceTapRouter(
        bus, fake_storage_provider.get_storage(WorkspaceSession)
    )
    await router.start()
    try:
        tick_sub = router.subscribe("W")
        try:
            # A delta must NOT wake the tick (cursor) subscriber.
            await bus.publish(
                f"session:s1{DELTA_EVENT_SUFFIX}",
                {"class": "text_delta", "part_id": "x:text", "delta": "hi"},
            )
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(tick_sub.__anext__(), timeout=0.2)
        finally:
            await tick_sub.aclose()
    finally:
        await router.aclose()
        await bus.aclose()
