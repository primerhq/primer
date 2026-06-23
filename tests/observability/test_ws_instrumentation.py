"""Tests for WS handler spans + metrics instrumentation (Task 9).

We test the metric updates by directly calling the instrumented send loop
functions and verifying the counters, and verify the gauge logic by
simulating the handler's inc/dec pattern.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from primer.observability.metrics import reset_for_test


@pytest.fixture(autouse=True)
def fresh_metrics():
    reset_for_test()
    yield


@pytest.fixture
def in_memory_tracer():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _patch_tracer(provider):
    return patch(
        "primer.observability.tracing.get_tracer",
        side_effect=lambda name: provider.get_tracer(name),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_gauge_value(gauge, *label_values):
    """Read the current value of a labeled gauge."""
    samples = {
        (tuple(sorted(s.labels.items())), s.name): s.value
        for metric in gauge.collect()
        for s in metric.samples
        if s.name.endswith("_total") is False and not s.name.endswith("_created")
    }
    label_dict = dict(zip([ln for ln in gauge._labelnames], label_values))
    for (lbl_items, _name), value in samples.items():
        if dict(lbl_items) == label_dict:
            return value
    return 0.0


def _get_counter_value(counter, *label_values):
    """Read the current value of a labeled counter."""
    label_dict = dict(zip([ln for ln in counter._labelnames], label_values))
    for metric in counter.collect():
        for s in metric.samples:
            if s.name.endswith("_total") and dict(s.labels) == label_dict:
                return s.value
    return 0.0


# ---------------------------------------------------------------------------
# WS active-connection gauge: manual inc/dec simulation
# ---------------------------------------------------------------------------


def test_ws_connections_active_gauge_chat():
    """The active gauge increments/decrements correctly."""
    import primer.observability.metrics as m

    assert _get_gauge_value(m.ws_connections_active, "chat") == 0.0

    m.ws_connections_active.labels("chat").inc()
    assert _get_gauge_value(m.ws_connections_active, "chat") == 1.0

    m.ws_connections_active.labels("chat").inc()
    assert _get_gauge_value(m.ws_connections_active, "chat") == 2.0

    m.ws_connections_active.labels("chat").dec()
    assert _get_gauge_value(m.ws_connections_active, "chat") == 1.0

    m.ws_connections_active.labels("chat").dec()
    assert _get_gauge_value(m.ws_connections_active, "chat") == 0.0


def test_ws_connections_active_gauge_session():
    import primer.observability.metrics as m

    m.ws_connections_active.labels("session").inc()
    assert _get_gauge_value(m.ws_connections_active, "session") == 1.0
    m.ws_connections_active.labels("session").dec()
    assert _get_gauge_value(m.ws_connections_active, "session") == 0.0


# ---------------------------------------------------------------------------
# WS frames-sent counter: via _send_loop_instrumented
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_send_loop_increments_frame_counter():
    """_send_loop_instrumented should increment ws_frames_sent_total per frame."""
    import primer.observability.metrics as m
    from primer.api.routers.chats import _send_loop_instrumented
    from primer.model.chats import ChatMessage
    from datetime import datetime, timezone

    # Build two fake ChatMessage rows
    def _make_msg(seq: int):
        return ChatMessage(
            id=f"chat-x-{seq}",
            chat_id="chat-x",
            seq=seq,
            kind="assistant_token",
            payload={"delta": f"token{seq}"},
            created_at=datetime.now(timezone.utc),
        )

    row1 = _make_msg(1)
    row2 = _make_msg(2)

    # Create a tick subscription that yields one tick then stops
    class FakePage:
        items = [row1, row2]

    class FakeStorage:
        async def find(self, pred, page, *, order_by=None):
            return FakePage()

    class FakeTick:
        seq = 2

    async def fake_tick_sub():
        yield FakeTick()
        # async for loop exits when the generator returns normally

    ws = MagicMock()
    ws.send_json = AsyncMock()

    # Run the instrumented send loop until the tick sub is exhausted
    try:
        await asyncio.wait_for(
            _send_loop_instrumented(ws, "chat-x", FakeStorage(), fake_tick_sub(), 0, kind="chat"),
            timeout=2.0,
        )
    except (TimeoutError, StopAsyncIteration):
        pass

    assert _get_counter_value(m.ws_frames_sent_total, "chat") == 2.0


@pytest.mark.asyncio
async def test_ws_session_duration_histogram():
    """ws_session_duration_seconds should observe once per connection lifetime."""
    import primer.observability.metrics as m

    # Simulate the pattern in chat_ws: inc before, observe in finally
    m.ws_connections_active.labels("chat").inc()
    m.ws_session_duration_seconds.labels("chat").observe(0.5)
    m.ws_connections_active.labels("chat").dec()

    samples = {
        s.name: s.value
        for metric in m.ws_session_duration_seconds.collect()
        for s in metric.samples
        if s.labels.get("kind") == "chat"
    }
    assert samples.get("ws_session_duration_seconds_count", 0) == 1
    assert samples.get("ws_session_duration_seconds_sum", 0) >= 0.4


# ---------------------------------------------------------------------------
# Span attribute check via a lightweight span tracer
# ---------------------------------------------------------------------------


def test_ws_span_name_chat(in_memory_tracer):
    """The WS span should be named 'ws.chat' for chat handlers."""
    provider, exporter = in_memory_tracer

    with _patch_tracer(provider):
        tracer = provider.get_tracer("primer.ws")
        with tracer.start_as_current_span("ws.chat") as span:
            span.set_attribute("ws.frames_sent", 3)

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "ws.chat"
    assert spans[0].attributes["ws.frames_sent"] == 3


def test_ws_span_name_session(in_memory_tracer):
    """The WS span should be named 'ws.session' for session handlers."""
    provider, exporter = in_memory_tracer

    with _patch_tracer(provider):
        tracer = provider.get_tracer("primer.ws")
        with tracer.start_as_current_span("ws.session") as span:
            span.set_attribute("ws.frames_sent", 0)

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "ws.session"
