"""Prometheus metrics registry for Primer.

All metrics are declared at module level, bound to a dedicated
:class:`prometheus_client.CollectorRegistry` instance (``registry``).
This means GET /metrics returns only Primer-defined metrics, not the
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
# Dedicated registry — isolates Primer metrics from the default process metrics
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
# Channel-event metrics
# ---------------------------------------------------------------------------

channel_events_normalized_total = Counter(
    "channel_events_normalized_total",
    "Total channel events normalized to a ChannelEvent, by normalized type and provider.",
    ["event_type", "provider"],
    registry=registry,
)
channel_events_matched_total = Counter(
    "channel_events_matched_total",
    "Total channel events that matched at least one binding's event_matcher, by normalized type and provider.",
    ["event_type", "provider"],
    registry=registry,
)
channel_events_dispatched_total = Counter(
    "channel_events_dispatched_total",
    "Total channel events whose matched binding dispatched an action, by normalized type and provider.",
    ["event_type", "provider"],
    registry=registry,
)
reply_binding_resolutions_total = Counter(
    "reply_binding_resolutions_total",
    "Total reply-binding resolutions, by winning scope (session / workspace / none).",
    ["scope"],
    registry=registry,
)


# ---------------------------------------------------------------------------
# Worker / turn / session metrics (S7)
# ---------------------------------------------------------------------------
#
# Turns and claim-lane tasks run for minutes, not the seconds the default
# prometheus buckets cover, so both duration histograms share one explicit
# bucket set.

_TASK_BUCKETS = (0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600)

worker_tasks_total = Counter(
    "worker_tasks_total",
    "Total claim-lane tasks run, by stable worker label, claim kind and outcome.",
    ["worker", "kind", "status"],
    registry=registry,
)

storage_cas_drift_total = Counter(
    "storage_cas_drift_total",
    "Storage.patch_if writes rejected although a fresh read still matches the guard "
    "(a serialization drift, not a race), by model.",
    ["model"],
    registry=registry,
)

worker_task_duration_seconds = Histogram(
    "worker_task_duration_seconds",
    "Claim-lane task duration in seconds, by worker, claim kind and outcome.",
    ["worker", "kind", "status"],
    buckets=_TASK_BUCKETS,
    registry=registry,
)

turns_total = Counter(
    "turns_total",
    "Total session turns, by binding ref (agent or graph id) and outcome.",
    ["binding_ref", "status"],
    registry=registry,
)

turn_duration_seconds = Histogram(
    "turn_duration_seconds",
    "Session turn duration in seconds, by binding ref and outcome.",
    ["binding_ref", "status"],
    buckets=_TASK_BUCKETS,
    registry=registry,
)

llm_calls_total = Counter(
    "llm_calls_total",
    "Total model calls at the agent-loop seam, by provider, profile and outcome.",
    ["provider_id", "profile_id", "status"],
    registry=registry,
)

llm_profile_tokens_total = Counter(
    "llm_profile_tokens_total",
    "Total LLM tokens by model profile and direction (in/out). The older "
    "llm_tokens_total keeps the provider-kind view.",
    ["profile_id", "direction"],
    registry=registry,
)

llm_prompt_estimate_ratio = Histogram(
    "llm_prompt_estimate_ratio",
    "The provider's reported input tokens divided by our character-heuristic estimate of the prompt that "
    "was sent, per model call that came back with usage, by provider. 1.0 is a perfect estimate; above 1 "
    "the heuristic undercounts (a compaction trigger running on it fires late).",
    ["provider_id"],
    buckets=(0.25, 0.4, 0.5, 0.6, 0.75, 0.9, 1.0, 1.1, 1.25, 1.5, 2.0, 3.0, 4.0),
    registry=registry,
)

llm_count_tokens_total = Counter(
    "llm_count_tokens_total",
    "Native token counts at the counter wrapper, by provider, what the figure "
    "stands on (source) and outcome (ok or why it fell back to an estimate).",
    ["provider_id", "source", "outcome"],
    registry=registry,
)

llm_count_tokens_seconds = Histogram(
    "llm_count_tokens_seconds",
    "Time one native token count took, in seconds, by provider and source.",
    ["provider_id", "source"],
    buckets=(0.005, 0.025, 0.1, 0.5, 1, 3, 10),
    registry=registry,
)

llm_tokenizer_ready = Gauge(
    "llm_tokenizer_ready",
    "1 when a tokenizer vocabulary is loaded and verified in this process, 0 when "
    "it is unavailable (native counts fall back to an estimate).",
    ["name"],
    registry=registry,
)

compaction_outcomes_total = Counter(
    "compaction_outcomes_total",
    "Compactions by outcome: pruned (tier 1 sufficed), summarised, unreducible "
    "(nothing could be summarised; no marker), skipped (the trigger cannot be reached and "
    "the prompt fits) or insufficient (summarised, still over the trigger).",
    ["outcome"],
    registry=registry,
)

sessions_active = Gauge(
    "sessions_active",
    "Sessions currently executing a turn, by workspace.",
    ["workspace_id"],
    registry=registry,
)

session_interrupts_via_poll_total = Counter(
    "session_interrupts_via_poll_total",
    "Stop requests the dispatch watcher found on the session row instead of on the bus. "
    "reason=queued_before_turn: the flag was already set when the turn began (a Stop "
    "recorded while the session was queued; not a fault). reason=missed_while_running: "
    "the Stop was requested while the turn ran and its bus message never arrived (a "
    "failed publish or a degraded bus); this is the one that means the bus is dropping Stops.",
    ["reason"],
    registry=registry,
)

session_interrupt_publish_failures_total = Counter(
    "session_interrupt_publish_failures_total",
    "Stop requests whose bus publish failed. The request is still recorded on the "
    "session row and the running worker recovers it by polling, so it is delayed, not lost.",
    registry=registry,
)

session_completed_turn_noop_total = Counter(
    "session_completed_turn_noop_total",
    "Session claims that found the previous turn completed (completed_turn_no == turn_no) "
    "but its release never committed (abandoned or rolled back), and released without "
    "calling the model again. Each one is a turn that would otherwise have run twice.",
    registry=registry,
)

tool_wait_malformed_scoped_id_total = Counter(
    "tool_wait_malformed_scoped_id_total",
    "Tool-call task ids that did not parse as a scoped id where a tool_wait wake key was needed, "
    "by site (adapter: the last-sibling release woke nothing; materializer, repark: that batch's key "
    "was left out of the park; dispatch: no batch's id parsed, so the park was not written and the "
    "turn ended failed). Any non-zero value is a bug in the id mint.",
    ["site"],
    registry=registry,
)


# ---------------------------------------------------------------------------
# Cardinality guard (S7 section 4, crosscheck m2)
# ---------------------------------------------------------------------------

ALLOWED_LABEL_NAMES = frozenset({
    # S7 taxonomy (12-s7-design.md section 4).
    "worker",
    "kind",
    "status",
    "binding_ref",
    "provider_id",
    "profile_id",
    "direction",
    "toolset",
    "tool",
    "workspace_id",
    # Pre-S7 house labels already on this registry.
    "provider",
    "error_type",
    "name",
    "outcome",
    "event_type",
    "scope",
    # What a token figure stands on (usage_calibrated/native/native_approx/
    # native_plus_estimated/estimate): a closed enum, so bounded.
    "source",
    # Entity model class name (storage_cas_drift_total): bounded by the set of model classes.
    "model",
    # Why a Stop was found on the session row (queued_before_turn / missed_while_running):
    # a closed enum, so bounded.
    "reason",
    # Which code site met a malformed scoped tool-call id (adapter / materializer / dispatch /
    # repark): a closed enum, so bounded.
    "site",
})
"""Every label name any Primer instrument is permitted to carry.

The guard test (tests/observability/test_label_allowlist.py) fails on any
label outside this set. session_id is deliberately absent: it is unbounded
by session volume, and session-scoped stats come from the derived timeline
(GET /sessions/{id}/turns/{n}/timeline) instead.
"""


def registered_label_names() -> set[str]:
    """Every label name on every instrument registered on ``registry``.

    Reads ``_labelnames`` off each collector rather than walking
    ``registry.collect()``: a labelled instrument that has never been
    incremented yields no samples, so collect() would silently miss it.
    """
    names: set[str] = set()
    for collector in list(registry._collector_to_names):  # noqa: SLF001
        for label in getattr(collector, "_labelnames", ()) or ():
            names.add(label)
    return names


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
    global channel_events_normalized_total, channel_events_matched_total  # noqa: PLW0603
    global channel_events_dispatched_total, reply_binding_resolutions_total  # noqa: PLW0603
    global worker_tasks_total, worker_task_duration_seconds  # noqa: PLW0603
    global storage_cas_drift_total  # noqa: PLW0603
    global turns_total, turn_duration_seconds  # noqa: PLW0603
    global llm_calls_total, llm_profile_tokens_total, sessions_active  # noqa: PLW0603
    global session_interrupts_via_poll_total  # noqa: PLW0603
    global session_interrupt_publish_failures_total  # noqa: PLW0603
    global session_completed_turn_noop_total  # noqa: PLW0603
    global llm_count_tokens_total, llm_count_tokens_seconds, llm_tokenizer_ready  # noqa: PLW0603
    global llm_prompt_estimate_ratio  # noqa: PLW0603
    global compaction_outcomes_total  # noqa: PLW0603
    global tool_wait_malformed_scoped_id_total  # noqa: PLW0603

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
    channel_events_normalized_total = Counter(
        "channel_events_normalized_total",
        "Total channel events normalized to a ChannelEvent, by normalized type and provider.",
        ["event_type", "provider"],
        registry=registry,
    )
    channel_events_matched_total = Counter(
        "channel_events_matched_total",
        "Total channel events that matched at least one binding's event_matcher, by normalized type and provider.",
        ["event_type", "provider"],
        registry=registry,
    )
    channel_events_dispatched_total = Counter(
        "channel_events_dispatched_total",
        "Total channel events whose matched binding dispatched an action, by normalized type and provider.",
        ["event_type", "provider"],
        registry=registry,
    )
    reply_binding_resolutions_total = Counter(
        "reply_binding_resolutions_total",
        "Total reply-binding resolutions, by winning scope (session / workspace / none).",
        ["scope"],
        registry=registry,
    )
    worker_tasks_total = Counter(
        "worker_tasks_total",
        "Total claim-lane tasks run, by stable worker label, claim kind and outcome.",
        ["worker", "kind", "status"],
        registry=registry,
    )
    storage_cas_drift_total = Counter(
        "storage_cas_drift_total",
        "Storage.patch_if writes rejected although a fresh read still matches the guard "
        "(a serialization drift, not a race), by model.",
        ["model"],
        registry=registry,
    )
    worker_task_duration_seconds = Histogram(
        "worker_task_duration_seconds",
        "Claim-lane task duration in seconds, by worker, claim kind and outcome.",
        ["worker", "kind", "status"],
        buckets=_TASK_BUCKETS,
        registry=registry,
    )
    turns_total = Counter(
        "turns_total",
        "Total session turns, by binding ref (agent or graph id) and outcome.",
        ["binding_ref", "status"],
        registry=registry,
    )
    turn_duration_seconds = Histogram(
        "turn_duration_seconds",
        "Session turn duration in seconds, by binding ref and outcome.",
        ["binding_ref", "status"],
        buckets=_TASK_BUCKETS,
        registry=registry,
    )
    llm_calls_total = Counter(
        "llm_calls_total",
        "Total model calls at the agent-loop seam, by provider, profile and outcome.",
        ["provider_id", "profile_id", "status"],
        registry=registry,
    )
    llm_profile_tokens_total = Counter(
        "llm_profile_tokens_total",
        "Total LLM tokens by model profile and direction (in/out). The older "
        "llm_tokens_total keeps the provider-kind view.",
        ["profile_id", "direction"],
        registry=registry,
    )
    llm_prompt_estimate_ratio = Histogram(
        "llm_prompt_estimate_ratio",
        "The provider's reported input tokens divided by our character-heuristic estimate of the prompt that "
        "was sent, per model call that came back with usage, by provider. 1.0 is a perfect estimate; above 1 "
        "the heuristic undercounts (a compaction trigger running on it fires late).",
        ["provider_id"],
        buckets=(0.25, 0.4, 0.5, 0.6, 0.75, 0.9, 1.0, 1.1, 1.25, 1.5, 2.0, 3.0, 4.0),
        registry=registry,
    )
    llm_count_tokens_total = Counter(
        "llm_count_tokens_total",
        "Native token counts at the counter wrapper, by provider, what the figure "
        "stands on (source) and outcome (ok or why it fell back to an estimate).",
        ["provider_id", "source", "outcome"],
        registry=registry,
    )
    llm_count_tokens_seconds = Histogram(
        "llm_count_tokens_seconds",
        "Time one native token count took, in seconds, by provider and source.",
        ["provider_id", "source"],
        buckets=(0.005, 0.025, 0.1, 0.5, 1, 3, 10),
        registry=registry,
    )
    llm_tokenizer_ready = Gauge(
        "llm_tokenizer_ready",
        "1 when a tokenizer vocabulary is loaded and verified in this process, 0 when "
        "it is unavailable (native counts fall back to an estimate).",
        ["name"],
        registry=registry,
    )
    compaction_outcomes_total = Counter(
        "compaction_outcomes_total",
        "Compactions by outcome: pruned (tier 1 sufficed), summarised, unreducible "
        "(nothing could be summarised; no marker), skipped (the trigger cannot be reached and "
        "the prompt fits) or insufficient (summarised, still over the trigger).",
        ["outcome"],
        registry=registry,
    )
    sessions_active = Gauge(
        "sessions_active",
        "Sessions currently executing a turn, by workspace.",
        ["workspace_id"],
        registry=registry,
    )
    session_interrupts_via_poll_total = Counter(
        "session_interrupts_via_poll_total",
        "Stop requests the dispatch watcher found on the session row instead of on the bus. "
        "reason=queued_before_turn: the flag was already set when the turn began (a Stop "
        "recorded while the session was queued; not a fault). reason=missed_while_running: "
        "the Stop was requested while the turn ran and its bus message never arrived (a "
        "failed publish or a degraded bus); this is the one that means the bus is dropping Stops.",
        ["reason"],
        registry=registry,
    )
    session_interrupt_publish_failures_total = Counter(
        "session_interrupt_publish_failures_total",
        "Stop requests whose bus publish failed. The request is still recorded on the "
        "session row and the running worker recovers it by polling, so it is delayed, not lost.",
        registry=registry,
    )
    session_completed_turn_noop_total = Counter(
        "session_completed_turn_noop_total",
        "Session claims that found the previous turn completed (completed_turn_no == turn_no) "
        "but its release never committed (abandoned or rolled back), and released without "
        "calling the model again. Each one is a turn that would otherwise have run twice.",
        registry=registry,
    )
    tool_wait_malformed_scoped_id_total = Counter(
        "tool_wait_malformed_scoped_id_total",
        "Tool-call task ids that did not parse as a scoped id where a tool_wait wake key was needed, "
        "by site (adapter: the last-sibling release woke nothing; materializer, repark: that batch's key "
        "was left out of the park; dispatch: no batch's id parsed, so the park was not written and the "
        "turn ended failed). Any non-zero value is a bug in the id mint.",
        ["site"],
        registry=registry,
    )


__all__ = [
    "registry",
    "reset_for_test",
    "ALLOWED_LABEL_NAMES",
    "registered_label_names",
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
    # Channel events
    "channel_events_normalized_total",
    "channel_events_matched_total",
    "channel_events_dispatched_total",
    "reply_binding_resolutions_total",
    # Worker / turn / session (S7)
    "worker_tasks_total",
    "worker_task_duration_seconds",
    "turns_total",
    "turn_duration_seconds",
    "llm_calls_total",
    "llm_profile_tokens_total",
    "compaction_outcomes_total",
    "sessions_active",
    # Stop (interrupt)
    "session_interrupts_via_poll_total",
    "session_interrupt_publish_failures_total",
    # Completed-turn re-claim guard
    "session_completed_turn_noop_total",
    # Tool-call claims
    "tool_wait_malformed_scoped_id_total",
]
