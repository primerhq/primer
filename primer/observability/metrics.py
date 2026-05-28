"""Prometheus metrics registry for Matrix.

All metrics are declared at module level, bound to a dedicated
:class:`prometheus_client.CollectorRegistry` instance (``registry``).
This means GET /metrics returns only Matrix-defined metrics, not the
default process/platform metrics that prometheus_client auto-registers
on the global registry.

Usage
-----
Import the named metric and call the prometheus_client API directly::

    from primer.observability.metrics import llm_tokens_total
    llm_tokens_total.labels(provider="anthropic", direction="in").inc(500)

Test isolation
--------------
Call :func:`reset_for_test` between tests to obtain a clean registry
with all counters zeroed.  This avoids counter accumulation across the
test suite.
"""

from __future__ import annotations

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
)

# ---------------------------------------------------------------------------
# Dedicated registry — isolates Matrix metrics from the default process metrics
# ---------------------------------------------------------------------------
registry = CollectorRegistry(auto_describe=True)

# ---------------------------------------------------------------------------
# LLM metrics
# ---------------------------------------------------------------------------

llm_tokens_total = Counter(
    "llm_tokens_total",
    "Total LLM tokens processed, partitioned by provider and direction (in/out).",
    ["provider", "direction"],
    registry=registry,
)

llm_duration_seconds = Histogram(
    "llm_duration_seconds",
    "LLM streaming call duration in seconds.",
    ["provider"],
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30),
    registry=registry,
)

llm_failure_total = Counter(
    "llm_failure_total",
    "Total LLM call failures, partitioned by provider and error type.",
    ["provider", "error_type"],
    registry=registry,
)

llm_retry_total = Counter(
    "llm_retry_total",
    "Total LLM call retries, partitioned by provider.",
    ["provider"],
    registry=registry,
)

# ---------------------------------------------------------------------------
# Tool call metrics
# ---------------------------------------------------------------------------

tool_calls_total = Counter(
    "tool_calls_total",
    "Total tool calls, partitioned by tool name and outcome (ok/fail).",
    ["name", "outcome"],
    registry=registry,
)

tool_duration_seconds = Histogram(
    "tool_duration_seconds",
    "Tool execution duration in seconds, partitioned by tool name.",
    ["name"],
    registry=registry,
)

# ---------------------------------------------------------------------------
# Claim / queue metrics
# ---------------------------------------------------------------------------

claim_enqueue_latency_seconds = Histogram(
    "claim_enqueue_latency_seconds",
    "Time a lease waited in the queue before being claimed, in seconds.",
    ["kind"],
    registry=registry,
)

claim_queue_depth = Gauge(
    "claim_queue_depth",
    "Current number of unclaimed leases in the queue, by kind.",
    ["kind"],
    registry=registry,
)

claim_active_count = Gauge(
    "claim_active_count",
    "Current number of active (claimed, not yet completed) leases, by kind.",
    ["kind"],
    registry=registry,
)

# ---------------------------------------------------------------------------
# WebSocket connection metrics
# ---------------------------------------------------------------------------

ws_connections_active = Gauge(
    "ws_connections_active",
    "Current number of active WebSocket connections, by kind.",
    ["kind"],
    registry=registry,
)

ws_frames_sent_total = Counter(
    "ws_frames_sent_total",
    "Total WebSocket frames sent, by kind.",
    ["kind"],
    registry=registry,
)

ws_session_duration_seconds = Histogram(
    "ws_session_duration_seconds",
    "WebSocket session duration in seconds, by kind.",
    ["kind"],
    registry=registry,
)

ws_replay_backlog_seconds = Histogram(
    "ws_replay_backlog_seconds",
    "Age of the oldest replayed event at WS connect time, in seconds.",
    ["kind"],
    registry=registry,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def reset_for_test() -> None:
    """Reset all metrics to a pristine state for test isolation.

    Creates a brand-new :class:`CollectorRegistry` and re-registers all
    named metric objects against it.  After this call every counter/gauge/
    histogram is zeroed and the module-level ``registry`` reference points
    to the fresh registry.

    Call this from a test fixture or ``setup_method`` before each test that
    exercises metrics to prevent accumulation across the suite.
    """
    global registry  # noqa: PLW0603
    global llm_tokens_total, llm_duration_seconds, llm_failure_total  # noqa: PLW0603
    global llm_retry_total  # noqa: PLW0603
    global tool_calls_total, tool_duration_seconds  # noqa: PLW0603
    global claim_enqueue_latency_seconds, claim_queue_depth  # noqa: PLW0603
    global claim_active_count  # noqa: PLW0603
    global ws_connections_active, ws_frames_sent_total  # noqa: PLW0603
    global ws_session_duration_seconds, ws_replay_backlog_seconds  # noqa: PLW0603

    registry = CollectorRegistry(auto_describe=True)

    llm_tokens_total = Counter(
        "llm_tokens_total",
        "Total LLM tokens processed, partitioned by provider and direction (in/out).",
        ["provider", "direction"],
        registry=registry,
    )
    llm_duration_seconds = Histogram(
        "llm_duration_seconds",
        "LLM streaming call duration in seconds.",
        ["provider"],
        buckets=(0.1, 0.5, 1, 2, 5, 10, 30),
        registry=registry,
    )
    llm_failure_total = Counter(
        "llm_failure_total",
        "Total LLM call failures, partitioned by provider and error type.",
        ["provider", "error_type"],
        registry=registry,
    )
    llm_retry_total = Counter(
        "llm_retry_total",
        "Total LLM call retries, partitioned by provider.",
        ["provider"],
        registry=registry,
    )
    tool_calls_total = Counter(
        "tool_calls_total",
        "Total tool calls, partitioned by tool name and outcome (ok/fail).",
        ["name", "outcome"],
        registry=registry,
    )
    tool_duration_seconds = Histogram(
        "tool_duration_seconds",
        "Tool execution duration in seconds, partitioned by tool name.",
        ["name"],
        registry=registry,
    )
    claim_enqueue_latency_seconds = Histogram(
        "claim_enqueue_latency_seconds",
        "Time a lease waited in the queue before being claimed, in seconds.",
        ["kind"],
        registry=registry,
    )
    claim_queue_depth = Gauge(
        "claim_queue_depth",
        "Current number of unclaimed leases in the queue, by kind.",
        ["kind"],
        registry=registry,
    )
    claim_active_count = Gauge(
        "claim_active_count",
        "Current number of active (claimed, not yet completed) leases, by kind.",
        ["kind"],
        registry=registry,
    )
    ws_connections_active = Gauge(
        "ws_connections_active",
        "Current number of active WebSocket connections, by kind.",
        ["kind"],
        registry=registry,
    )
    ws_frames_sent_total = Counter(
        "ws_frames_sent_total",
        "Total WebSocket frames sent, by kind.",
        ["kind"],
        registry=registry,
    )
    ws_session_duration_seconds = Histogram(
        "ws_session_duration_seconds",
        "WebSocket session duration in seconds, by kind.",
        ["kind"],
        registry=registry,
    )
    ws_replay_backlog_seconds = Histogram(
        "ws_replay_backlog_seconds",
        "Age of the oldest replayed event at WS connect time, in seconds.",
        ["kind"],
        registry=registry,
    )


__all__ = [
    "registry",
    "reset_for_test",
    # LLM
    "llm_tokens_total",
    "llm_duration_seconds",
    "llm_failure_total",
    "llm_retry_total",
    # Tools
    "tool_calls_total",
    "tool_duration_seconds",
    # Claims
    "claim_enqueue_latency_seconds",
    "claim_queue_depth",
    "claim_active_count",
    # WebSockets
    "ws_connections_active",
    "ws_frames_sent_total",
    "ws_session_duration_seconds",
    "ws_replay_backlog_seconds",
]
