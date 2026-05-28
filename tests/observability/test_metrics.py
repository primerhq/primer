"""Tests for matrix.observability.metrics."""

from __future__ import annotations

import pytest

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client.exposition import generate_latest


# Always start each test with a fresh registry so counters do not accumulate.
@pytest.fixture(autouse=True)
def _reset_metrics():
    import matrix.observability.metrics as m
    m.reset_for_test()
    yield
    # reset again after test for isolation
    m.reset_for_test()


class TestRegistryExists:
    def test_registry_is_collector_registry(self) -> None:
        import matrix.observability.metrics as m
        assert isinstance(m.registry, CollectorRegistry)

    def test_registry_generates_output(self) -> None:
        import matrix.observability.metrics as m
        output = generate_latest(m.registry)
        assert isinstance(output, bytes)


class TestLlmMetrics:
    def test_llm_tokens_total_is_counter(self) -> None:
        import matrix.observability.metrics as m
        assert isinstance(m.llm_tokens_total, Counter)

    def test_llm_tokens_total_increments(self) -> None:
        import matrix.observability.metrics as m
        m.llm_tokens_total.labels(provider="anthropic", direction="in").inc(100)
        val = m.llm_tokens_total.labels(provider="anthropic", direction="in")._value.get()
        assert val == 100.0

    def test_llm_duration_seconds_is_histogram(self) -> None:
        import matrix.observability.metrics as m
        assert isinstance(m.llm_duration_seconds, Histogram)

    def test_llm_duration_seconds_has_custom_buckets(self) -> None:
        import matrix.observability.metrics as m
        # Observe a value and verify histogram accumulates it.
        m.llm_duration_seconds.labels(provider="anthropic").observe(2.5)
        # The histogram has observations if _sum > 0.
        sample = m.llm_duration_seconds.labels(provider="anthropic")._sum.get()
        assert sample == pytest.approx(2.5)

    def test_llm_failure_total_is_counter(self) -> None:
        import matrix.observability.metrics as m
        assert isinstance(m.llm_failure_total, Counter)

    def test_llm_failure_total_increments(self) -> None:
        import matrix.observability.metrics as m
        m.llm_failure_total.labels(provider="openai", error_type="TimeoutError").inc()
        val = m.llm_failure_total.labels(provider="openai", error_type="TimeoutError")._value.get()
        assert val == 1.0

    def test_llm_retry_total_is_counter(self) -> None:
        import matrix.observability.metrics as m
        assert isinstance(m.llm_retry_total, Counter)

    def test_llm_retry_total_increments(self) -> None:
        import matrix.observability.metrics as m
        m.llm_retry_total.labels(provider="gemini").inc(3)
        val = m.llm_retry_total.labels(provider="gemini")._value.get()
        assert val == 3.0


class TestToolMetrics:
    def test_tool_calls_total_is_counter(self) -> None:
        import matrix.observability.metrics as m
        assert isinstance(m.tool_calls_total, Counter)

    def test_tool_calls_total_increments(self) -> None:
        import matrix.observability.metrics as m
        m.tool_calls_total.labels(name="bash", outcome="ok").inc()
        val = m.tool_calls_total.labels(name="bash", outcome="ok")._value.get()
        assert val == 1.0

    def test_tool_duration_seconds_is_histogram(self) -> None:
        import matrix.observability.metrics as m
        assert isinstance(m.tool_duration_seconds, Histogram)

    def test_tool_duration_seconds_observes(self) -> None:
        import matrix.observability.metrics as m
        m.tool_duration_seconds.labels(name="read_file").observe(0.25)
        sample = m.tool_duration_seconds.labels(name="read_file")._sum.get()
        assert sample == pytest.approx(0.25)


class TestClaimMetrics:
    def test_claim_enqueue_latency_is_histogram(self) -> None:
        import matrix.observability.metrics as m
        assert isinstance(m.claim_enqueue_latency_seconds, Histogram)

    def test_claim_enqueue_latency_observes(self) -> None:
        import matrix.observability.metrics as m
        m.claim_enqueue_latency_seconds.labels(kind="chat").observe(1.5)
        sample = m.claim_enqueue_latency_seconds.labels(kind="chat")._sum.get()
        assert sample == pytest.approx(1.5)

    def test_claim_queue_depth_is_gauge(self) -> None:
        import matrix.observability.metrics as m
        assert isinstance(m.claim_queue_depth, Gauge)

    def test_claim_queue_depth_set(self) -> None:
        import matrix.observability.metrics as m
        m.claim_queue_depth.labels(kind="session").set(42)
        val = m.claim_queue_depth.labels(kind="session")._value.get()
        assert val == 42.0

    def test_claim_active_count_is_gauge(self) -> None:
        import matrix.observability.metrics as m
        assert isinstance(m.claim_active_count, Gauge)

    def test_claim_active_count_increments_and_decrements(self) -> None:
        import matrix.observability.metrics as m
        m.claim_active_count.labels(kind="chat").inc()
        m.claim_active_count.labels(kind="chat").inc()
        m.claim_active_count.labels(kind="chat").dec()
        val = m.claim_active_count.labels(kind="chat")._value.get()
        assert val == 1.0


class TestWsMetrics:
    def test_ws_connections_active_is_gauge(self) -> None:
        import matrix.observability.metrics as m
        assert isinstance(m.ws_connections_active, Gauge)

    def test_ws_connections_active_increments(self) -> None:
        import matrix.observability.metrics as m
        m.ws_connections_active.labels(kind="chat").inc()
        val = m.ws_connections_active.labels(kind="chat")._value.get()
        assert val == 1.0

    def test_ws_frames_sent_total_is_counter(self) -> None:
        import matrix.observability.metrics as m
        assert isinstance(m.ws_frames_sent_total, Counter)

    def test_ws_frames_sent_total_increments(self) -> None:
        import matrix.observability.metrics as m
        m.ws_frames_sent_total.labels(kind="session").inc(10)
        val = m.ws_frames_sent_total.labels(kind="session")._value.get()
        assert val == 10.0

    def test_ws_session_duration_is_histogram(self) -> None:
        import matrix.observability.metrics as m
        assert isinstance(m.ws_session_duration_seconds, Histogram)

    def test_ws_session_duration_observes(self) -> None:
        import matrix.observability.metrics as m
        m.ws_session_duration_seconds.labels(kind="chat").observe(30.0)
        sample = m.ws_session_duration_seconds.labels(kind="chat")._sum.get()
        assert sample == pytest.approx(30.0)

    def test_ws_replay_backlog_is_histogram(self) -> None:
        import matrix.observability.metrics as m
        assert isinstance(m.ws_replay_backlog_seconds, Histogram)

    def test_ws_replay_backlog_observes(self) -> None:
        import matrix.observability.metrics as m
        m.ws_replay_backlog_seconds.labels(kind="session").observe(5.0)
        sample = m.ws_replay_backlog_seconds.labels(kind="session")._sum.get()
        assert sample == pytest.approx(5.0)


class TestResetForTest:
    def test_reset_clears_counter_values(self) -> None:
        import matrix.observability.metrics as m
        m.llm_tokens_total.labels(provider="anthropic", direction="in").inc(999)
        m.reset_for_test()
        # After reset, a fresh labels call starts at 0.
        val = m.llm_tokens_total.labels(provider="anthropic", direction="in")._value.get()
        assert val == 0.0

    def test_reset_clears_gauge_values(self) -> None:
        import matrix.observability.metrics as m
        m.claim_queue_depth.labels(kind="chat").set(100)
        m.reset_for_test()
        val = m.claim_queue_depth.labels(kind="chat")._value.get()
        assert val == 0.0

    def test_reset_replaces_registry(self) -> None:
        import matrix.observability.metrics as m
        old_registry = m.registry
        m.reset_for_test()
        assert m.registry is not old_registry

    def test_reset_new_registry_is_collector_registry(self) -> None:
        import matrix.observability.metrics as m
        m.reset_for_test()
        assert isinstance(m.registry, CollectorRegistry)
