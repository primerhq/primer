"""Session-related Pydantic models for the Workspace abstraction.

A session is one execution of one agent on one workspace. The same
agent can run many sessions on the same workspace; each session has
its own state slot under ``.state/sessions/<session-id>/`` and its own
truncation cache subdirectory under ``.tmp/<session-id>/``.

Models exported:

* :class:`SessionStatus` -- lifecycle enum (running / waiting / paused / ended).
* :class:`SessionInfo` -- serialisable summary, persisted as
  ``.state/sessions/<session-id>/session.json``.
* :class:`AgentBinding` -- snapshot of agent metadata captured at
  session start, persisted as
  ``.state/sessions/<session-id>/agent.json``.
* :class:`WaitingState` -- discriminated union describing what a
  ``WAITING`` session is blocked on, persisted as
  ``.state/sessions/<session-id>/waiting.json`` only when
  ``status == WAITING``.
* :class:`Instruction` -- one user-supplied turn appended to a running
  session.

See ``docs/superpowers/specs/2026-05-02-workspace-design.md`` for the
full design.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum, StrEnum
from typing import TYPE_CHECKING, Annotated, Any, Literal, Union

from pydantic import BaseModel, Field

from primer.model.common import Identifiable

if TYPE_CHECKING:
    from primer.model.agent import Agent
    from primer.model.graph import Graph


# ===========================================================================
# Session status
# ===========================================================================


class SessionStatus(str, Enum):
    """Lifecycle state of an :class:`AgentSession`.

    :attr:`CREATED` is the pre-execution state — the session row exists
    but no worker has been told to run it yet. ``POST .../resume`` (or
    ``auto_start=True`` on session create) signals the scheduler to
    transition into :attr:`RUNNING`.

    Transitions out of :attr:`CREATED` / between non-terminal states
    are driven by the agent runtime (which sets :attr:`RUNNING` while a
    turn is in flight, :attr:`WAITING` when blocked on either user
    input or an approval, :attr:`ENDED` on terminal) and by the user
    (who can request :attr:`PAUSED` and resume back to :attr:`RUNNING`).

    :attr:`WAITING` is intentionally one state regardless of what the
    session is blocked on -- the distinction is recorded in
    ``.state/sessions/<id>/waiting.json`` via the :class:`WaitingState`
    discriminated union, which the user inspects to figure out what
    response is needed.

    Terminal: :attr:`ENDED`. Non-terminal: everything else.
    """

    CREATED = "created"
    RUNNING = "running"
    WAITING = "waiting"
    PAUSED = "paused"
    ENDED = "ended"


# ===========================================================================
# Waiting state (discriminated union)
# ===========================================================================


class _UserInputWaiting(BaseModel):
    """Session is waiting for the user to respond to a question."""

    kind: Literal["user_input"] = Field(
        default="user_input",
        description="Discriminator tag identifying this as a user-input wait.",
    )
    prompt: str = Field(
        ...,
        min_length=1,
        description="The prompt the agent emitted to the user.",
    )
    queued_at: datetime = Field(
        ...,
        description="UTC instant the wait began.",
    )


class _ToolApprovalWaiting(BaseModel):
    """Session is waiting for the user to approve / deny a pending tool call."""

    kind: Literal["tool_approval"] = Field(
        default="tool_approval",
        description="Discriminator tag identifying this as a tool-approval wait.",
    )
    tool_id: str = Field(
        ...,
        min_length=1,
        description="The tool the agent wants to invoke.",
    )
    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description="The arguments the agent wants to pass to the tool.",
    )
    rationale: str | None = Field(
        default=None,
        description="Optional explanation the agent provided for the request.",
    )
    queued_at: datetime = Field(
        ...,
        description="UTC instant the wait began.",
    )


WaitingState = Annotated[
    _UserInputWaiting | _ToolApprovalWaiting,
    Field(discriminator="kind"),
]
"""Type alias: a discriminated union describing what a ``WAITING`` session
is blocked on.

Discriminated by the ``kind`` field so Pydantic can parse the state
from an untyped dict (e.g. JSON loaded from ``waiting.json``) without
ambiguity. Forward-compatible: future variants
(``_NetworkAccessWaiting``, ``_FileWriteApprovalWaiting``, etc.) can be
added without changing :class:`SessionStatus` or the storage layout --
the runtime just writes a new ``kind`` into ``waiting.json``.
"""


# ===========================================================================
# Agent binding
# ===========================================================================


class AgentBinding(BaseModel):
    """Snapshot of agent metadata captured at session start.

    Persisted as ``.state/sessions/<session_id>/agent.json``. Tells
    anyone inspecting the session slot which agent is executing
    without forcing them to consult the live agent registry (which may
    have evolved since the session started -- agent definitions can
    change, agents can be deleted, etc.).

    Intentionally minimal in v1: just identity and the registered tool
    list. Future revisions can expand to include the agent's system
    prompt snapshot, model id, and any other state needed to fully
    reproduce the session.
    """

    agent_id: str = Field(
        ...,
        min_length=1,
        description="Identifier of the agent executing this session.",
    )
    agent_name: str = Field(
        ...,
        min_length=1,
        description="Human-readable agent name at session start.",
    )
    registered_tool_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Tool ids the agent had registered (first-class) at session "
            "start. Workspace tools are NOT included -- they are "
            "composed onto the agent at session start by the runtime "
            "and are listed by inspecting the workspace's tool set."
        ),
    )


# ===========================================================================
# Session info
# ===========================================================================


class SessionInfo(BaseModel):
    """Serialisable summary of an :class:`AgentSession`.

    What :meth:`primer.int.Workspace.list_sessions` returns. Persisted
    as ``.state/sessions/<session_id>/session.json`` so it survives
    workspace restart.
    """

    session_id: str = Field(..., min_length=1)
    agent_id: str = Field(..., min_length=1)
    workspace_id: str = Field(..., min_length=1)
    status: SessionStatus = Field(
        ...,
        description="Current lifecycle state of the session.",
    )
    ended_reason: Literal[
        "completed", "failed", "cancelled", "workspace_lost", "force_deleted"
    ] | None = Field(
        default=None,
        description=(
            "Set when ``status == SessionStatus.ENDED``. ``None`` "
            "otherwise. ``completed`` for a clean exit, ``failed`` for "
            "an unrecoverable error, ``cancelled`` when the user "
            "requested the end."
        ),
    )
    parent_session_id: str | None = Field(
        default=None,
        description=(
            "If this session was spawned by another (the agent runtime's "
            "spawn meta-tool), the parent's id. Used for history "
            "attribution; no automatic propagation of state happens."
        ),
    )
    started_at: datetime = Field(
        ...,
        description="UTC instant the session was created.",
    )
    last_activity_at: datetime = Field(
        ...,
        description="UTC instant of the most recent state-mutating event.",
    )
    ended_at: datetime | None = Field(
        default=None,
        description="UTC instant the session entered the ENDED state, if any.",
    )
    initial_instructions: str | None = Field(
        default=None,
        description=(
            "The user-supplied prompt at session start, if any. "
            "Recorded for inspection; the actual delivery to the agent "
            "happens via the first user-role message in messages.jsonl."
        ),
    )


# ===========================================================================
# Instruction
# ===========================================================================


class Instruction(BaseModel):
    """One user-supplied instruction appended to a running session.

    Written directly into ``messages.jsonl`` as a user-role message at
    append time; the next agent turn picks it up via the standard
    "messages since last assistant turn" mechanism. No separate queue
    file -- the messages log IS the queue. This model is the
    user-facing record of one such append, returned from
    :meth:`AgentSession.append_instruction` for caller bookkeeping.
    """

    instruction_id: str = Field(
        ...,
        min_length=1,
        description="Unique identifier for this instruction.",
    )
    session_id: str = Field(
        ...,
        min_length=1,
        description="Session the instruction was appended to.",
    )
    content: str = Field(
        ...,
        min_length=1,
        description="The instruction text the user supplied.",
    )
    queued_at: datetime = Field(
        ...,
        description="UTC instant the instruction was committed to state.",
    )


# ===========================================================================
# Persisted Session entity (scheduler-visible)
# ===========================================================================


class AgentSessionBinding(BaseModel):
    """Bind a persisted Session to a single Agent (discriminated-union member).

    Distinct from the existing on-disk :class:`AgentBinding` snapshot —
    this one identifies which Agent a scheduler-managed Session is
    bound to, with an optional frozen snapshot field for immutability
    against later edits to the Agent row.
    """

    kind: Literal["agent"] = Field(
        default="agent",
        description="Discriminator tag for the SessionBinding union.",
    )
    agent_id: str = Field(..., min_length=1)
    agent_snapshot: "Agent | None" = Field(
        default=None,
        description=(
            "Optional frozen snapshot of the Agent definition at session "
            "start. Insulates a long-running session from later edits to "
            "the Agent row."
        ),
    )


class GraphSessionBinding(BaseModel):
    """Bind a persisted Session to a single Graph (discriminated-union member)."""

    kind: Literal["graph"] = Field(
        default="graph",
        description="Discriminator tag for the SessionBinding union.",
    )
    graph_id: str = Field(..., min_length=1)
    graph_snapshot: "Graph | None" = Field(
        default=None,
        description=(
            "Optional frozen snapshot of the Graph definition at session "
            "start. Insulates a long-running session from later edits to "
            "the Graph row."
        ),
    )


SessionBinding = Annotated[
    AgentSessionBinding | GraphSessionBinding,
    Field(discriminator="kind"),
]


class WorkspaceSession(Identifiable):
    """Persisted session row — scheduler's source of truth.

    Distinct from :class:`SessionInfo`, which is the on-disk projection
    inside the workspace's ``.state/`` repo. The two are synchronised
    at turn boundaries; divergence is permitted for at most one turn
    (at-least-once trade-off documented in the spec at
    docs/superpowers/specs/2026-05-10-background-execution-scheduler-design.md).
    """

    workspace_id: str = Field(..., min_length=1)
    binding: SessionBinding
    status: SessionStatus
    parent_session_id: str | None = Field(default=None)
    initial_instructions: str | None = Field(default=None)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    started_at: datetime | None = Field(default=None)
    last_turn_at: datetime | None = Field(default=None)
    ended_at: datetime | None = Field(default=None)
    ended_reason: Literal[
        "completed", "failed", "cancelled", "workspace_lost", "force_deleted"
    ] | None = Field(
        default=None,
    )
    ended_detail: str | None = Field(
        default=None,
        description=(
            "Free-text refinement of ``ended_reason``. Populated by graph "
            "execution paths to carry codes like 'begin_input_invalid', "
            "'end_output_invalid', 'routing_failed', 'template_error', "
            "'max_iterations_exceeded' that don't warrant new "
            "ended_reason literals."
        ),
    )

    # Fence + scheduler-visible columns
    turn_no: int = Field(default=0, ge=0)
    last_worker_id: str | None = Field(default=None)

    # Cancel/pause request flags (set by API, read by worker)
    pause_requested: bool = Field(default=False)
    cancel_requested: bool = Field(default=False)

    # ----------------------------------------------------------------------
    # Yielding-tool park state (M1 of the yielding-tools feature).
    # See docs/superpowers/specs/2026-05-22-yielding-tools-design.md §5.
    # ----------------------------------------------------------------------
    parked_status: Literal["parked", "resumable"] | None = Field(
        default=None,
        description=(
            "Park lifecycle: NULL when not parked, 'parked' when the "
            "session is waiting for its yield event to fire, "
            "'resumable' when the event has fired (or a timeout/cancel "
            "synthesised one) and the next worker claim should resume "
            "the parked turn. Excluded from the claim-loop while "
            "'parked'."
        ),
    )
    parked_event_key: str | None = Field(
        default=None,
        description=(
            "Routing key for the event bus. NULL when not parked. "
            "Conventional prefixes: 'timer:', 'ask_user:', 'watch:', "
            "'mcp_task:'."
        ),
    )
    parked_event_keys: list[str] | None = Field(
        default=None,
        description=(
            "Multi-event park: the full set of event keys this session "
            "is waiting on (any one firing wakes it). NULL for the common "
            "single-event park, which uses parked_event_key alone."
        ),
    )
    parked_until: datetime | None = Field(
        default=None,
        description=(
            "Deadline at which the park auto-resumes with a "
            "YieldTimeout payload. Drives both the timer scheduler "
            "(timer:* parks) and the global timeout sweeper."
        ),
    )
    parked_at: datetime | None = Field(
        default=None,
        description=(
            "Timestamp the park was written. Resume uses this to "
            "compute elapsed_seconds for the tool's resume hook."
        ),
    )
    parked_state: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Opaque blob carrying the in-progress turn state "
            "(LLM message history, pending tool_call_id, tool name, "
            "yield resume_metadata, and on resume the merged "
            "resume_event_payload). Shape documented in spec §5.2."
        ),
    )

    # ------------------------------------------------------------------
    # Streaming lifecycle fields (mirrors Chat model in primer.model.chats).
    # See docs/superpowers/specs/2026-05-27-workspace-session-streaming-design.md.
    # ------------------------------------------------------------------
    last_seq: int = Field(
        default=0,
        description=(
            "Highest sequence number assigned to a session message record "
            "for this session.  Authoritative cursor for cursor-replay on "
            "WS reconnect; the WS endpoint emits records with "
            "``seq > cursor`` in order.  Bumped atomically by the "
            "message writer (see primer.session.persistence)."
        ),
    )
    turn_status: Literal["idle", "claimable", "running"] = Field(
        default="idle",
        description=(
            "Lifecycle state of the FIFO queue + worker claim. "
            "``idle`` means no pending work.  ``claimable`` means a "
            "user instruction has landed (or a parked session just became "
            "resumable) and a worker should pick the session up. "
            "``running`` means a worker holds the claim and is actively "
            "processing.  Orthogonal to :attr:`parked_status` — a claimed "
            "session that parks on a yielding tool keeps ``turn_status`` "
            "where it is while ``parked_status`` flips."
        ),
    )
    cancel_requested_at: datetime | None = Field(
        default=None,
        description=(
            "Set by the API when an ``interrupt`` WS frame arrives. "
            "The owning worker polls the field via heartbeat reads "
            "AND subscribes to a ``session:{id}:cancel`` bus event for "
            "faster wake-up.  Cleared by the worker after honouring "
            "the cancellation."
        ),
    )
    pause_requested_at: datetime | None = Field(
        default=None,
        description=(
            "Set by the API when a pause request arrives.  The owning "
            "worker drains the current event and then releases the claim "
            "without advancing to the next turn.  Cleared on resume."
        ),
    )


# ===========================================================================
# Session message record
# ===========================================================================


class SessionMessageKind(StrEnum):
    """Wire-level message kinds emitted by the session executor.

    Mirrors :data:`primer.model.chats.ChatMessageKind` for the workspace
    session streaming surface.  Each record in the per-session message
    log carries the kind plus a kind-specific ``payload`` JSON blob.
    """

    USER_INPUT = "user_input"
    ASSISTANT_TOKEN = "assistant_token"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    YIELDED = "yielded"
    RESUMED = "resumed"
    DONE = "done"
    CANCELLED = "cancelled"
    ERROR = "error"
    # Graph-runtime node lifecycle: one record at node ENTER and one at
    # node EXIT during a graph run. Shared 1:1 with
    # :class:`primer.tap.event.TapEventClass.GRAPH_TRANSITION` so these flow
    # through the existing tap unchanged. Payload shape:
    # ``{"node_id": str, "node_kind": str, "phase": "enter"|"exit",
    #    "status": str | None}`` (status populated on exit with the node
    # outcome, None on enter).
    GRAPH_TRANSITION = "graph_transition"


class SessionMessageRecord(BaseModel):
    """One row in the per-session append-only message log.

    Mirrors :class:`primer.model.chats.ChatMessage` for the workspace
    session streaming surface.  ``seq`` is monotonically increasing per
    session; the composite ``(session_id, seq)`` is the natural primary
    key (the storage layer composes an ``id`` from these two).
    """

    seq: int = Field(..., ge=1)
    kind: SessionMessageKind
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


# ===========================================================================
# Forward-reference resolution
# ===========================================================================
#
# AgentSessionBinding.agent_snapshot and GraphSessionBinding.graph_snapshot
# reference Agent / Graph, which we import only under TYPE_CHECKING above to
# avoid a circular import (primer.model.graph already imports SessionStatus
# from this module). Pydantic v2 needs concrete classes to build the schema,
# so we resolve the forward refs lazily inside a deferred-import helper that
# runs after this module has finished executing.

def _rebuild_models() -> None:
    # When this module is imported AS PART OF primer.model.graph loading
    # (graph.py imports SessionStatus from us before it finishes
    # defining Graph), the `from primer.model.graph import Graph` below
    # would raise ImportError. Swallow it; pydantic will rebuild on
    # first model use via the lazy resolver below.
    try:
        from primer.model.agent import Agent  # noqa: F401
        from primer.model.graph import Graph  # noqa: F401
    except ImportError:
        return

    AgentSessionBinding.model_rebuild()
    GraphSessionBinding.model_rebuild()
    WorkspaceSession.model_rebuild()


_rebuild_models()


# ===========================================================================
# Re-exports
# ===========================================================================


__all__ = [
    "AgentBinding",
    "AgentSessionBinding",
    "GraphSessionBinding",
    "Instruction",
    "SessionMessageKind",
    "SessionMessageRecord",
    "WorkspaceSession",
    "SessionBinding",
    "SessionInfo",
    "SessionStatus",
    "WaitingState",
]
