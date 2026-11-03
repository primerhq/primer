"""Scheduler-related configuration models.

Three things live here:

* :class:`RuntimeMode` — what the running process should do (serve API,
  run worker pool, or both).
* :class:`WorkerConfig` — knobs for the in-process worker pool.
* :class:`SchedulerProviderConfig` — discriminated union selecting the
  :class:`Scheduler` implementation (Postgres for production,
  in-memory for tests + single-process dev).
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, model_validator


class RuntimeMode(str, Enum):
    """What this process is responsible for."""

    API = "api"
    WORKER = "worker"
    API_PLUS_WORKER = "api+worker"


class WorkerConfig(BaseModel):
    """In-process worker pool knobs.

    The validator enforces ``lease_ttl_seconds >= 2 * heartbeat_interval_seconds``
    so a single missed heartbeat doesn't expire a lease.
    """

    concurrency: int = Field(default=8, ge=1, le=128)
    claim_batch_size: int = Field(default=4, ge=1, le=64)
    max_parallel_nodes: int = Field(
        default=16,
        ge=1,
        le=256,
        description=(
            "Per-superstep admission bound on concurrently-running graph "
            "nodes. A wide fan-out (map/broadcast) still runs every ready "
            "node, but at most this many at once — each node is an LLM loop "
            "plus workspace persistence, so an unbounded fan-out would spawn "
            "unbounded concurrent agent tasks. Bounds task/memory fan-out; "
            "the per-provider RateLimiter separately bounds LLM concurrency."
        ),
    )
    heartbeat_interval_seconds: int = Field(default=10, ge=1, le=60)
    lease_ttl_seconds: int = Field(default=30, ge=5, le=300)
    poll_interval_seconds: float = Field(default=2.0, ge=0.1, le=30.0)
    deltas_in_process_only: bool = Field(
        default=False,
        description=(
            "Send the live token-delta and phase frames of a turn to this process's own subscribers only, with no cross-process publish (on Postgres, "
            "no NOTIFY). Set it ONLY when this process also serves every tap / SSE reader of the sessions it runs (one API+worker process): in a "
            "split API / worker deployment, or with a second API replica, live tokens would stop reaching readers in the other processes. Ticks and "
            "every other event are unaffected. Default off: frames cross processes."
        ),
    )
    drain_timeout_seconds: int = Field(default=120, ge=1)
    max_attempts: int = Field(default=5, ge=1, le=100)
    base_backoff_seconds: float = Field(default=2.0, ge=0.1)
    max_backoff_seconds: float = Field(default=300.0, ge=1.0)
    worker_label: str | None = Field(
        default=None,
        description=(
            "Stable metric label for this process's worker pool. None "
            "derives it from the host name plus worker_index. This is "
            "never the lease-ownership id: that stays a per-start uuid so "
            "two pools on one host cannot claim each other's leases."
        ),
    )
    worker_index: int = Field(
        default=0,
        ge=0,
        le=1023,
        description=(
            "Ordinal distinguishing worker processes that share a host. "
            "Used only to build the stable metric label."
        ),
    )
    session_log_append_only: bool = Field(
        default=False,
        description=(
            "Task 14 stage 2. Write a session's messages.jsonl by appending only the new lines (an O_APPEND write under the session's messages "
            "lock, staged and committed as that file's delta) instead of reading the whole file and writing it back for every turn, steer and "
            "compaction marker. The file's bytes and the commit history (count, subjects, files per commit) are identical either way; "
            "only the I/O differs. A state repo that cannot append (a sandbox) keeps writing the whole file. Default OFF until it has soaked."
        ),
    )
    session_log_segment_bytes: int = Field(
        default=0,
        ge=0,
        description=(
            "Segment a session's message log: when its active segment file holds at least this many bytes, "
            "the next commit continues the log in a new file, so a closed segment is never written again and each commit stores only the small "
            "active one. Rotation is only ever done by the state commit, between whole batches of records. 0 (the default) never rotates; existing "
            "segments are always read. See docs/superpowers/specs/2026-11-02-segmented-session-log-design.md."
        ),
    )
    session_log_verify_reads: bool = Field(
        default=False,
        description=(
            "Compare every cached read of a session's messages.jsonl with a full parse and fail loudly on a mismatch (Task 14 stage 1). "
            "Costs one full parse per read, so it is for tests and a canary instance; the cache itself is always on."
        ),
    )
    parallel_readonly_tool_calls: bool = Field(
        default=False,
        description=(
            "When a model emits several tool calls in one message and EVERY one is a tool declared read-only (Tool.read_only), without an "
            "approval policy, and not yielding, notifying or uninterruptible, run them concurrently and answer in call order. Any other "
            "batch runs one call after another, exactly as before. Default OFF: today's serial order is documented behaviour (a Stop and an "
            "approval reason about it), so the gain is opt-in."
        ),
    )
    tool_calls_as_claims_enabled: bool = Field(
        default=False,
        description=(
            "Phase 3 stage 7a (docs/superpowers/2026-08-29-"
            "phase3-execution-topology-design.md, 01a0518b, user-approved). "
            "When enabled, each tool call of a batch becomes its own "
            "independently-claimable ToolCallTask instead of executing "
            "sequentially in-process, so a single gated call (approval "
            "required, or a yielding tool) no longer blocks its batch "
            "siblings, and the session parks until the batch is done. Default "
            "OFF. It controls ONLY whether the dispatch seam creates tasks. "
            "The executor that runs them is not built yet (stage 7a is in "
            "progress), so on this build nothing runs a task: leave it off "
            "outside development. Enabling it also selects the reserved "
            "TOOL_CALL claim slice (see "
            "tool_call_reserved_concurrency, derived when unset). A "
            "deployment with more than one worker process must use docker or "
            "kubernetes workspaces: the local-provider workspace write lock "
            "is in-process only."
        ),
    )

    tool_call_reserved_concurrency: int | None = Field(
        default=None,
        ge=1,
        le=127,
        description=(
            "Phase 3 stage 7a (01a0518b) pool-class separation (leader "
            "ruling C: 'build the minimal form into 7a's first cut'). "
            "None (default) = no reservation; ClaimKind.TOOL_CALL shares "
            "the single general pool exactly like every other kind does "
            "today. A value carves this many slots EXCLUSIVELY out of "
            "concurrency for TOOL_CALL claims: the pool loop then calls "
            "claim_due up to twice per iteration (kinds=[everything but "
            "TOOL_CALL] for the remaining concurrency - reserve slots, "
            "kinds=[TOOL_CALL] for the reserve, the latter only while a "
            "TOOL_CALL handler is registered) so a burst of tool-call "
            "tasks cannot starve session/harness/trigger claiming, or "
            "vice versa - the starvation risk identified in the 7a "
            "ground-truth remap (a worker holding a pool slot for a "
            "delegate-tool task can block on an untimed provider-slot "
            "acquire). Only meaningful when "
            "tool_calls_as_claims_enabled=True; ignored otherwise (the pool "
            "selects the reserved loop only when the flag is on). When the "
            "flag is on and this is unset it is DERIVED as "
            "max(1, concurrency // 2) (and stays unset when concurrency is "
            "1). The reserve is an EXCLUSIVE cap: with concurrency 3 the "
            "derived reserve is 1, so one tool task runs at a time per "
            "worker."
        ),
    )

    @model_validator(mode="after")
    def _lease_ttl_at_least_2x_heartbeat(self) -> "WorkerConfig":
        if self.lease_ttl_seconds < 2 * self.heartbeat_interval_seconds:
            raise ValueError(
                f"lease_ttl_seconds ({self.lease_ttl_seconds}) must be "
                f">= 2 * heartbeat_interval_seconds "
                f"({self.heartbeat_interval_seconds}) to tolerate one "
                "missed beat"
            )
        return self

    @model_validator(mode="after")
    def _derive_tool_call_reserve_when_the_flag_is_on(self) -> "WorkerConfig":
        """With the flag on the reserved TOOL_CALL slice is the whole mechanism, so derive it.

        ``max(1, concurrency // 2)``, never leaving fewer than one general slot: with
        ``concurrency == 1`` nothing is derived (the unreserved loop runs). An explicit value is
        kept as given; the next validator rejects one that would leave no general slot.
        """
        if (
            self.tool_calls_as_claims_enabled
            and self.tool_call_reserved_concurrency is None
            and self.concurrency > 1
        ):
            self.tool_call_reserved_concurrency = max(1, self.concurrency // 2)
        return self

    @model_validator(mode="after")
    def _tool_call_reserve_leaves_general_capacity(self) -> "WorkerConfig":
        if (
            self.tool_call_reserved_concurrency is not None
            and self.tool_call_reserved_concurrency >= self.concurrency
        ):
            raise ValueError(
                f"tool_call_reserved_concurrency "
                f"({self.tool_call_reserved_concurrency}) must be < "
                f"concurrency ({self.concurrency}) - at least one slot "
                "must remain for session/harness/trigger claiming"
            )
        return self


class SchedulerProviderType(str, Enum):
    POSTGRES = "postgres"
    IN_MEMORY = "in_memory"


class PostgresSchedulerConfig(BaseModel):
    """Knobs for the Postgres scheduler impl.

    Reuses the :class:`StorageProvider`'s connection pool — no DB
    parameters here. ``listen_reconnect_seconds`` controls the backoff
    when the dedicated LISTEN connection drops.
    """

    listen_reconnect_seconds: float = Field(default=2.0, ge=0.1, le=60.0)


class InMemorySchedulerConfig(BaseModel):
    """No knobs — present only for the discriminated-union shape."""


class SchedulerProviderConfig(BaseModel):
    """Discriminated config selecting the Scheduler impl.

    Mirrors the shape of :class:`primer.model.provider.StorageProviderConfig`
    so the factory pattern is identical.
    """

    provider: SchedulerProviderType
    config: PostgresSchedulerConfig | InMemorySchedulerConfig


__all__ = [
    "InMemorySchedulerConfig",
    "PostgresSchedulerConfig",
    "RuntimeMode",
    "SchedulerProviderConfig",
    "SchedulerProviderType",
    "WorkerConfig",
]
