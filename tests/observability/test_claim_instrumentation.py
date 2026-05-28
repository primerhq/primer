"""Tests for claim engine span + metrics instrumentation (Task 8)."""

from __future__ import annotations

from datetime import datetime, UTC, timedelta
from unittest.mock import patch

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from matrix.observability.metrics import reset_for_test


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
        "matrix.observability.tracing.get_tracer",
        side_effect=lambda name: provider.get_tracer(name),
    )


# ---------------------------------------------------------------------------
# InMemoryClaimEngine helpers
# ---------------------------------------------------------------------------


def _make_in_memory_engine():
    from matrix.claim.in_memory import InMemoryClaimEngine
    from matrix.int.claim import ClaimAdapter, ClaimKind, ReleaseOutcome

    class FakeChatAdapter(ClaimAdapter):
        kind = ClaimKind.CHAT
        entity_table = "chats"

        def eligibility_sql(self) -> str:
            return "SELECT id FROM chats WHERE status = 'active'"

        async def on_release(self, conn, entity_id, *, outcome: ReleaseOutcome):
            pass

    return InMemoryClaimEngine(adapters={ClaimKind.CHAT: FakeChatAdapter()})


# ---------------------------------------------------------------------------
# Tests: InMemory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_due_span_attributes(in_memory_tracer):
    provider, exporter = in_memory_tracer
    from matrix.int.claim import ClaimKind

    engine = _make_in_memory_engine()

    # Insert 3 leases
    for i in range(3):
        await engine.upsert(ClaimKind.CHAT, f"chat-{i}")

    with _patch_tracer(provider):
        leases = await engine.claim_due("worker-1", max_count=10)

    assert len(leases) == 3

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "claim.due"
    assert span.attributes.get("claim.count") == 3


@pytest.mark.asyncio
async def test_claim_due_latency_histogram(in_memory_tracer):
    provider, exporter = in_memory_tracer
    import matrix.observability.metrics as m
    from matrix.int.claim import ClaimKind

    engine = _make_in_memory_engine()
    await engine.upsert(ClaimKind.CHAT, "chat-0")
    await engine.upsert(ClaimKind.CHAT, "chat-1")

    with _patch_tracer(provider):
        leases = await engine.claim_due("worker-1", max_count=10)

    assert len(leases) == 2

    # Latency histogram should have 2 observations (one per lease)
    samples = {
        s.name: s.value
        for metric in m.claim_enqueue_latency_seconds.collect()
        for s in metric.samples
        if s.labels.get("kind") == "chat"
    }
    count = samples.get("claim_enqueue_latency_seconds_count", 0)
    assert count == 2, f"expected 2 observations; samples={samples}"


@pytest.mark.asyncio
async def test_claim_due_span_events_per_lease(in_memory_tracer):
    provider, exporter = in_memory_tracer
    from matrix.int.claim import ClaimKind

    engine = _make_in_memory_engine()
    await engine.upsert(ClaimKind.CHAT, "chat-0")
    await engine.upsert(ClaimKind.CHAT, "chat-1")

    with _patch_tracer(provider):
        leases = await engine.claim_due("worker-1", max_count=10)

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    # Should have one span event per claimed lease
    assert len(spans[0].events) == 2
    for event in spans[0].events:
        assert event.name == "claim_assigned"
        assert event.attributes.get("kind") == "chat"


@pytest.mark.asyncio
async def test_claim_due_empty_returns_zero_count(in_memory_tracer):
    provider, exporter = in_memory_tracer
    from matrix.int.claim import ClaimKind

    engine = _make_in_memory_engine()
    # No leases registered

    with _patch_tracer(provider):
        leases = await engine.claim_due("worker-1", max_count=10)

    assert leases == []
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].attributes.get("claim.count") == 0


@pytest.mark.asyncio
async def test_claim_due_enqueue_latency_positive(in_memory_tracer):
    """Leases past their next_attempt_at should record positive wait time."""
    provider, exporter = in_memory_tracer
    import matrix.observability.metrics as m
    from matrix.int.claim import ClaimKind

    engine = _make_in_memory_engine()
    # Register a lease that was due 5 seconds ago
    past = datetime.now(UTC) - timedelta(seconds=5)
    await engine.upsert(ClaimKind.CHAT, "chat-old", next_attempt_at=past)

    with _patch_tracer(provider):
        leases = await engine.claim_due("worker-1", max_count=10)

    assert len(leases) == 1

    # Sum should be approximately 5 seconds (at least 4 to account for test timing)
    samples = {
        s.name: s.value
        for metric in m.claim_enqueue_latency_seconds.collect()
        for s in metric.samples
        if s.labels.get("kind") == "chat"
    }
    latency_sum = samples.get("claim_enqueue_latency_seconds_sum", 0)
    assert latency_sum >= 4.0, f"expected latency >= 4s; got {latency_sum}"
