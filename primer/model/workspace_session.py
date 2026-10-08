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

from pydantic import BaseModel, Field, computed_field, field_validator

from primer.model.agent import _validate_response_format_schema
from primer.model.common import Identifiable
from primer.model.principal import PrincipalRef
from primer.model.storage import FieldRef, Op, Predicate, Value

if TYPE_CHECKING:
    from primer.model.agent import Agent
    from primer.model.graph import Graph


# ===========================================================================
# Session status
# ===========================================================================


class LastTurnError(BaseModel):
    """How the session's last turn failed: the code and the time (``WorkspaceSession.last_turn_error``)."""

    code: str = Field(
        ...,
        description=(
            "Why the turn failed: the model call's own code (``server_error``, ``rate_limit``, ``network_error``, ``auth_error``, the timeouts, "
            "``bad_request``...) when it was a model call, ``llm_stream_error`` when the stream gave none, ``turn_failed`` for a turn that raised "
            "something that is not a model error."
        ),
    )
    at: datetime = Field(..., description="When the turn's failure was recorded (UTC).")


class SessionStatus(str, Enum):
    """Lifecycle state of an :class:`AgentSession`.

    :attr:`CREATED` is the pre-execution state - the session row exists
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


SessionState = Literal["waiting", "running", "parked", "ended"]
"""The served vocabulary of :attr:`WorkspaceSession.session_state`."""


def NON_ENDED_STATUSES() -> list[str]:
    """Every :class:`SessionStatus` value but ``ended``, for a ``patch_if`` ``where`` status term.

    A FUNCTION despite its constant-style name: call it (``NON_ENDED_STATUSES()``); the bare name is the function
    object, which a ``where`` would reject. The name is kept so the call sites read like the status sets they stand
    for; a later change may rename both.

    Computed from the enum on each call, never hand-written, so a status added later is included: a write guarded on
    a typed list would silently refuse a row in the new status, where today's ``update_unless(status != ENDED)``
    accepts it. Returns a fresh list each call.
    """
    return [s.value for s in SessionStatus if s is not SessionStatus.ENDED]


def NON_ENDED_STATUSES_NOT_PAUSED() -> list[str]:
    """Every :class:`SessionStatus` value but ``ended`` and ``paused``, computed from the enum on each call.

    A FUNCTION despite its constant-style name: call it (``NON_ENDED_STATUSES_NOT_PAUSED()``), see
    :func:`NON_ENDED_STATUSES`.

    For a write that must also refuse a row that became ``paused`` after it was read (a ``/pause`` writes PAUSED
    directly on a WAITING or CREATED row, and a claim would resume a row it armed). Returns a fresh list each call.
    """
    return [s.value for s in SessionStatus if s not in (SessionStatus.ENDED, SessionStatus.PAUSED)]


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
    allow_external_tools: bool = Field(
        default=False,
        description=(
            "Snapshot of Agent.allow_external_tools at session start, so "
            "a mid-session agent edit never changes a running "
            "conversation's external-tool behaviour."
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
    binding: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Current binding, served from the DB row: {kind, agent_id | "
            "graph_id, profile_id, binding_epoch}. The on-disk session.json "
            "projection is rewritten only at the next turn boundary, so "
            "this field is what readers trust. agent_id above stays "
            "populated but is display-legacy once a session has switched."
        ),
    )
    name: str | None = Field(
        default=None,
        description=(
            "Optional user-supplied friendly name for the session. When set, "
            "the console shows it instead of the opaque ``sess-<hex>`` id. "
            "Backward-compatible: session.json files written before this "
            "field existed simply default to ``None``."
        ),
    )
    status: SessionStatus = Field(
        ...,
        description="Current lifecycle state of the session.",
    )
    ended_reason: Literal[
        "completed", "failed", "cancelled", "workspace_lost", "force_deleted", "tool_turn_cap"
    ] | None = Field(
        default=None,
        description=(
            "Set when ``status == SessionStatus.ENDED``. ``None`` "
            "otherwise. ``completed`` for a clean exit, ``failed`` for "
            "an unrecoverable error, ``cancelled`` when the user "
            "requested the end, ``tool_turn_cap`` when an autonomous "
            "session's turn was stopped by the agent's ``max_tool_turns`` "
            "(an interactive session rests WAITING instead)."
        ),
    )
    ended_detail: str | None = Field(
        default=None,
        description=(
            "Refinement of ``ended_reason`` (for example ``never_started``). Served from the DB row, which is the one truth "
            "about how a session ended: the runtime does not set it, so ``session.json`` carries ``null`` here and a reader "
            "that wants the real value overlays the row (``primer.session.slot_view``)."
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

    Distinct from the existing on-disk :class:`AgentBinding` snapshot -
    this one identifies which Agent a scheduler-managed Session is
    bound to, with an optional frozen snapshot field for immutability
    against later edits to the Agent row.
    """

    kind: Literal["agent"] = Field(
        default="agent",
        description="Discriminator tag for the SessionBinding union.",
    )
    agent_id: str = Field(..., min_length=1)
    profile_id: str | None = Field(
        default=None,
        description=(
            "Optional ModelProfile override for this run. ``None`` (the "
            "default) uses the agent's own ``model.profile_id``. Lets the "
            "same agent definition run against a different model or a "
            "different reasoning setting without duplicating the agent. "
            "Trigger-driven fresh sessions inherit this for free because "
            "they route through the same session factory."
        ),
    )
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
    profile_id: str | None = Field(
        default=None,
        description=(
            "Optional ModelProfile override for this run, mirroring "
            ":attr:`AgentSessionBinding.profile_id`. ``None`` leaves each "
            "agent node to resolve its own profile. Both binding kinds "
            "carry the override so a switch can change the model and the "
            "target in one gesture."
        ),
    )
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
    """Persisted session row - scheduler's source of truth.

    Distinct from :class:`SessionInfo`, which is the on-disk projection
    inside the workspace's ``.state/`` repo. The two are synchronised
    at turn boundaries; divergence is permitted for at most one turn
    (at-least-once trade-off documented in the spec at
    docs/superpowers/specs/2026-05-10-background-execution-scheduler-design.md).
    """

    workspace_id: str = Field(..., min_length=1)
    binding: SessionBinding
    binding_epoch: int = Field(
        default=0,
        ge=0,
        description=(
            "Monotonic counter bumped every time :attr:`binding` is "
            "reapplied. A session is an agent-independent workstream, so "
            "the binding is a mutable pointer; the epoch fences writes "
            "against it. Terminal-turn and park-resume writes carry the "
            "epoch they started under and are void when it no longer "
            "matches the row, which is what keeps a switch from being "
            "clobbered by work that began under the previous binding."
        ),
    )
    status: SessionStatus
    name: str | None = Field(
        default=None,
        description=(
            "Optional user-supplied friendly name, mirrored onto the on-disk "
            "SessionInfo (``session.json``). ``None`` falls back to the id in "
            "the console. Backward-compatible default for existing rows."
        ),
    )
    parent_session_id: str | None = Field(default=None)
    initial_instructions: str | None = Field(default=None)
    metadata: dict[str, Any] = Field(default_factory=dict)
    autonomous: bool | None = Field(
        default=None,
        description=(
            "Interactive-vs-autonomous control signal (studio-agents-interact "
            "§8.1). None => derive from binding kind (graph ⇒ autonomous, "
            "agent ⇒ interactive). True marks an agent self-driving loop as "
            "autonomous; False forces interactive. Read via "
            "primer.session.autonomy.session_is_autonomous."
        ),
    )
    initiated_by: PrincipalRef | None = Field(
        default=None,
        description=(
            "Persisted projection of the Principal that created this session "
            "(§8.2). Read back on worker/scheduler resume to populate "
            "ctx.identity so the originating identity survives the async "
            "boundary. None on historical rows -> readers fall back to the "
            "system principal (PrincipalRef.system())."
        ),
    )
    _validate_response_format = field_validator("response_format")(
        _validate_response_format_schema
    )

    created_at: datetime
    started_at: datetime | None = Field(default=None)
    last_turn_at: datetime | None = Field(default=None)
    ended_at: datetime | None = Field(default=None)
    ended_reason: Literal[
        "completed", "failed", "cancelled", "workspace_lost", "force_deleted", "tool_turn_cap"
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
    completed_turn_no: int | None = Field(
        default=None,
        description=(
            "The turn_no of the last turn whose effects are all committed. "
            "Written by run_one_session_turn as the LAST write of the "
            "terminal lock block of a turn that ran (the clean completion "
            "and the Stop/Cancel exit; never a failed turn, a park, an "
            "early exit or a row another path ended meanwhile), after last_seq and the drain cursor, and BEFORE "
            "the release whose transaction bumps turn_no. Cleared by "
            "nothing. It equals turn_no only between that write and the "
            "release's commit, so a claim that finds the two equal knows "
            "the previous turn completed and its release never committed "
            "(abandoned or rolled back): it takes the no-op path instead of "
            "calling the model again, and its own release applies the lost "
            "bump. Additive/optional, no migration: a row without it reads "
            "None, and an older build ignores the key."
        ),
    )
    last_worker_id: str | None = Field(default=None)

    # Cancel/pause request flags (set by API, read by worker)
    pause_requested: bool = Field(default=False)
    cancel_requested: bool = Field(default=False)
    interrupt_requested: bool = Field(
        default=False,
        description=(
            "Set by POST .../interrupt (Stop). The worker running the turn "
            "stops it at its next wait for the model (the first token, or "
            "between chunks; a tool call that is already running finishes "
            "first) and transitions the session to WAITING (alive/idle) "
            "instead of ENDED, so the user can keep chatting "
            "(studio-agents-interact §4.4). The worker learns of it from a "
            "bus message and, as a durable fallback, by re-reading this "
            "flag every couple of seconds while the turn runs. Never set "
            "on a parked session (the route refuses it), and cleared by "
            "the worker, by resuming a park, and by a human's later message."
        ),
    )

    workspace_refusal: str | None = Field(
        default=None,
        description=(
            "Why this session's turn could not run: the deployment REFUSES its workspace (a workspace on a "
            "local provider where the topology forbids one, ticket 01a1072f). Written when a turn meets the "
            "refusal, which pauses the session instead of ending it, and cleared when a turn starts or the "
            "session is resumed. The text lives on the row because messages.jsonl lives INSIDE the refused "
            "workspace and cannot be written. A refused session is intact and resumable once its workspace "
            "moves to a docker or kubernetes provider; it is never workspace_lost. Additive/optional, no "
            "migration: a row without it reads None, and an older build ignores the key."
        ),
    )

    last_turn_error: LastTurnError | None = Field(
        default=None,
        description=(
            "Set when the session's last turn FAILED, with the failure's code and time; cleared when the next turn starts and when a session is "
            "reopened. It is how a row that is not ended still says that the turn it rests after failed: a failed turn does not bump ``turn_no`` and "
            "its release drops the lease, so without it a resting session whose first turn failed looks like one that never started (the "
            "stuck-session sweeper reads it for that), and the console reads it for its failed-turn indicator. Written by the failure exit with one "
            "field-scoped patch. Additive/optional, no migration: a row without it reads None, and an older build ignores the key."
        ),
    )

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
    # Literal[True] | None, never a bool: a cleared row stores NULL, so a selector
    # or partial index on the marker sees only marked rows whether it tests
    # `= true` or IS NOT NULL. A stored False would be a second "unset" spelling
    # (an IS NOT NULL index would hold every cleared row).
    parked_tool_batches: Literal[True] | None = Field(
        default=None,
        description=(
            "Park-batch marker: will be set to true when parked_state "
            "references at least one tool_wait batch (tool calls parked as "
            "claimable tasks) and left NULL otherwise. Cleared by writing "
            "NULL, never false. A selector only: readiness is always "
            "recomputed from the task rows, never trusted from this flag."
        ),
    )
    resumable_at: datetime | None = Field(
        default=None,
        description=(
            "When the park was last flipped to resumable (stamped by the "
            "wake): the anchor for a grace period measured from the wake "
            "(parked_at is the park time, the wrong anchor)."
        ),
    )
    external_tools: list[dict[str, Any]] | None = Field(
        default=None,
        description=(
            "Active invoker-supplied tool defs for the next/current turn "
            "(ExternalToolDef dumps, wire alias 'schema'). Replaced by "
            "each turn-triggering invocation; None/[] means the turn has "
            "no external tools. A pure tool_results body leaves the set "
            "untouched (the resumed turn keeps its tools)."
        ),
    )

    # ------------------------------------------------------------------
    # Streaming lifecycle fields.
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
    pending_binding_switch: dict[str, Any] | None = Field(
        default=None,
        description=(
            "A binding switch requested while a turn was active. Shape: "
            "{kind: 'agent'|'graph', agent_id?|graph_id?, profile_id?, "
            "actor}. Applied and cleared at the next drain checkpoint, "
            "BEFORE any queued steer is realized, so the follow-up runs "
            "under the incoming binding rather than the one it was "
            "waiting behind."
        ),
    )
    response_format: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Persistent per-session structured-output JSON Schema. "
            "Overrides the agent default for this session. An ephemeral "
            "per-steer override rides "
            "metadata['ephemeral_response_format'] and beats this for "
            "exactly one turn. Mirrors Chat.response_format."
        ),
    )
    next_unprocessed_seq: int = Field(
        default=0,
        ge=0,
        description=(
            "Drain checkpoint cursor: the seq the next turn scan starts "
            "from. Advanced only at fully-drained checkpoints, to the "
            "row's own last_seq + 1 re-read fresh under the lifecycle "
            "lock, and only ever forwards. Advancing it mid-turn let a "
            "crash replay records the previous turn had already "
            "consumed, which is what the chat drain got wrong. "
            "count_turn_state's max_seen_seq is the separate tool for "
            "callers that already hold the log lines, not for this "
            "cursor."
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
            "processing.  Orthogonal to :attr:`parked_status` - a claimed "
            "session that parks on a yielding tool keeps ``turn_status`` "
            "where it is while ``parked_status`` flips."
        ),
    )
    turn_started_at: datetime | None = Field(
        default=None,
        description=(
            "Wall-clock time the current turn set turn_status='running' "
            "(dispatch.run_one_session_turn, just before build_executor). "
            "Cleared back to None in the same write that returns "
            "turn_status to 'idle'. Additive/optional so existing rows "
            "with no value simply read None. Exists for crash-safety "
            "staleness heuristics: a worker that dies mid-turn (hard "
            "crash, OOM kill, pod eviction) never reaches the cleanup "
            "write, so a row can be observed at turn_status='running' "
            "with no live worker behind it - callers that need to detect "
            "that should compare this timestamp's age against a "
            "plausible turn-duration ceiling rather than trusting "
            "turn_status alone."
        ),
    )
    agent_phase: Literal[
        "thinking", "responding", "executing", "waiting",
    ] | None = Field(
        default=None,
        description=(
            "Finer-grained sub-state of an in-flight turn, written by "
            "dispatch.run_one_session_turn as it watches the executor's "
            "StreamEvents (primer.session.persistence.translate_stream_event "
            "already sees the same events at the same layer). 'thinking' "
            "covers request-sent-through-reasoning-tokens (a request just "
            "sent, or ReasoningDelta events - reasoning is rendered as its "
            "own collapsible block, not the final answer, so it counts as "
            "thinking); 'responding' starts at the first non-reasoning "
            "TextDelta; 'executing' starts at ToolCallStart and reverts to "
            "'thinking' once the tool result is appended (the agent is "
            "about to re-request a completion); 'waiting' is the value "
            "between turns / on park / on turn end - the same moments "
            "turn_status returns to 'idle'. Additive/optional, no "
            "migration: existing rows simply read None (equivalent to "
            "'waiting'). None whenever turn_status is 'idle' - only "
            "meaningful while a turn is genuinely running, mirroring "
            "turn_started_at's own scope."
        ),
    )
    agent_phase_turn_no: int | None = Field(
        default=None,
        description=(
            "turn_no this agent_phase value belongs to - the fence a "
            "reader compares against the row's own turn_no to detect a "
            "stale write (e.g. a delayed phase update from a turn that "
            "already ended, arriving after the NEXT turn already started; "
            "readers should ignore agent_phase when this doesn't match "
            "turn_no). Additive/optional, no migration."
        ),
    )
    agent_phase_stamped_at: datetime | None = Field(
        default=None,
        description=(
            "Wall-clock time agent_phase was last written. Same "
            "crash-safety role as turn_started_at (staleness heuristic "
            "for a worker that died between phase transitions), scoped to "
            "the finer-grained phase signal rather than the coarser "
            "turn_status. Additive/optional, no migration."
        ),
    )
    cancel_requested_at: datetime | None = Field(
        default=None,
        description=(
            "When the most recent Stop (POST .../interrupt) or Cancel was "
            "requested. Informational: the worker does not read this field "
            "(it reads ``interrupt_requested`` and ``cancel_requested``) and "
            "does not clear it when a request is honoured; only a session "
            "reset clears it."
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

    @computed_field  # type: ignore[prop-decorator]
    @property
    def session_state(self) -> SessionState:
        """One served truth, derived from the three stored axes.

        The stored axes (``status``, ``parked_status``, ``turn_status``)
        stay exactly as they are - three separate columns, unchanged
        write paths - this is purely a read-time projection, computed
        fresh on every serialisation, never stored, never accepted as
        input (a bare ``@property`` under ``@computed_field`` has no
        setter, so passing ``session_state=`` into the constructor raises
        same as any other unknown kwarg). Because it lives on
        WorkspaceSession itself rather than a response subclass, every
        route that serialises a WorkspaceSession (or a subclass like
        SessionDetail) gets it automatically - no per-route wiring, no
        route that could forget to add it.

        Precedence (checked in this order - see each branch for why):
        1. ``status == ENDED`` -> "ended". Terminal; nothing else matters
           once true.
        2. ``parked_status in ("parked", "resumable")`` -> "parked".
           Checked before turn_status because the park write and the
           turn_status="idle" cleanup write are NOT atomic with each
           other (dispatch.run_one_session_turn's YieldToWorker branch
           writes the park columns; the streaming-phase `finally` clears
           turn_status moments later) - a reader could observe
           turn_status still "running" for a session that has already
           parked. Parked is the more specific/accurate transient truth.
        3. ``turn_status == "running"`` -> "running". The real signal
           01a04d91-a7a0 introduced - see that field's own docstring for
           why this used to always read "idle" here instead.
        4. ``status in (WAITING, PAUSED) and turn_no > 0`` -> "parked"
           (01a0518a). A completed-turn-rests-parked flip
           (``_CLEAN_TURN_RESTS_PARKED`` in primer.session.dispatch)
           means a clean stop now leaves the row at WAITING, not ENDED -
           but WAITING is ALSO the value for two OTHER, older cases
           (the executor's assistant-asked-a-question heuristic;
           max_tokens/content_filter) that write the identical
           (status=WAITING, ended_reason=None) shape, so there is no
           stored field that distinguishes "just rested" from "blocked
           on a specific answer" from "always been idle". Rather than
           add a new column to encode a distinction the served
           vocabulary was never that fine-grained about, ``turn_no > 0``
           answers the question this vocabulary actually needs: "has
           this session ever completed a turn?" - CREATED sessions
           (never claimed, turn_no == 0) are genuinely fresh and stay
           "waiting"; anything resting after at least one turn (WAITING
           for any of the three reasons above, or an operator PAUSED
           mid-flight) reads "parked" - resuming both looks the same to
           a caller (send a new message; wake_session's existing
           ``_RESUMABLE`` set already includes WAITING and PAUSED, see
           that module) and the richer per-reason distinction (a real
           question needing an answer vs. a turn just finished) is
           exactly what the UI's own ``describeSessionState()``
           (ui/components/session-state.jsx) already computes from
           ``parked_event_keys``/``ended_reason`` for display - this
           field was never meant to replace that, only to give it (and
           every other consumer) one clean signal for "is a turn
           genuinely in flight right now".
        5. Otherwise -> "waiting". CREATED (turn_no == 0, never started)
           and turn_status idle/claimable with turn_no == 0 - a session
           alive but genuinely never having produced a turn to rest
           after.
        """
        if self.status == SessionStatus.ENDED:
            return "ended"
        if self.parked_status is not None:
            return "parked"
        if self.turn_status == "running":
            return "running"
        if (
            self.status in (SessionStatus.WAITING, SessionStatus.PAUSED)
            and self.turn_no > 0
        ):
            return "parked"
        return "waiting"


def _compare(field: str, op: Op, value: Any = None) -> Predicate:
    return Predicate(left=FieldRef(name=field), op=op, right=Value(value=value))


def _all_of(*parts: Predicate) -> Predicate:
    out = parts[0]
    for part in parts[1:]:
        out = Predicate(left=out, op=Op.AND, right=part)
    return out


def _any_of(*parts: Predicate) -> Predicate:
    out = parts[0]
    for part in parts[1:]:
        out = Predicate(left=out, op=Op.OR, right=part)
    return out


def session_state_predicate(state: SessionState) -> Predicate:
    """The storage predicate matching exactly the rows whose ``session_state`` reads *state*.

    Filtering by the derived state has to be a second statement of
    :attr:`WorkspaceSession.session_state`'s rule, because the state is
    computed on read and never stored. Both are written over the same four
    axes in the same precedence (``status`` ended, then ``parked_status``, then
    ``turn_status == "running"``, then a resting WAITING/PAUSED session with
    ``turn_no > 0``, otherwise waiting), and
    ``tests/storage/test_session_state_filter_parity.py`` stores every
    combination of them and requires the two to agree row for row on every
    backend: change the rule in one place and that test fails until the other
    follows.

    "Not running" is spelled ``turn_status IS NULL OR turn_status != 'running'``
    (and "not resting" likewise for ``turn_no``): a row written before an axis
    existed has no such key, the model reads its default, and in SQL a bare
    ``!=`` against the missing key is NULL, which would drop the row from every
    state.

    Do not filter on the stored ``session_state`` key instead: ``model_dump``
    writes a snapshot of it on a whole-row write, and a field-scoped patch of
    an axis does not refresh it, so it is whatever the last whole-row write
    computed.
    """
    ended = _compare("status", Op.EQ, SessionStatus.ENDED.value)
    not_ended = _compare("status", Op.NE, SessionStatus.ENDED.value)
    parked_axis = _compare("parked_status", Op.IS_NOT_NULL)
    no_parked_axis = _compare("parked_status", Op.IS_NULL)
    turn_running = _compare("turn_status", Op.EQ, "running")
    turn_not_running = _any_of(
        _compare("turn_status", Op.IS_NULL),
        _compare("turn_status", Op.NE, "running"),
    )
    resting = _all_of(
        _any_of(
            _compare("status", Op.EQ, SessionStatus.WAITING.value),
            _compare("status", Op.EQ, SessionStatus.PAUSED.value),
        ),
        _compare("turn_no", Op.GT, 0),
    )
    not_resting = _any_of(
        _all_of(
            _compare("status", Op.NE, SessionStatus.WAITING.value),
            _compare("status", Op.NE, SessionStatus.PAUSED.value),
        ),
        _compare("turn_no", Op.IS_NULL),
        _compare("turn_no", Op.LE, 0),
    )
    if state == "ended":
        return ended
    if state == "running":
        return _all_of(not_ended, no_parked_axis, turn_running)
    if state == "parked":
        return _all_of(
            not_ended, _any_of(parked_axis, _all_of(turn_not_running, resting)),
        )
    if state == "waiting":
        return _all_of(not_ended, no_parked_axis, turn_not_running, not_resting)
    raise ValueError(f"unknown session state {state!r}")


# ===========================================================================
# Session message record
# ===========================================================================


class SessionMessageKind(StrEnum):
    """Wire-level message kinds emitted by the session executor.

    The record vocabulary for the workspace
    session streaming surface.  Each record in the per-session message
    log carries the kind plus a kind-specific ``payload`` JSON blob.
    """

    USER_INPUT = "user_input"
    ASSISTANT_TOKEN = "assistant_token"
    # Model reasoning / thinking text, streamed alongside the answer by
    # providers that expose it. Persisted for DISPLAY only: it is skipped
    # when rebuilding the prompt, because replaying a model's own
    # reasoning back to it is either rejected outright or degrades the
    # next turn.
    REASONING = "reasoning"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    # Push-frame for an invoker-supplied (external) tool call, written
    # when a turn soft-yields on one so a live client sees the call
    # immediately and again on reconnect replay. Display/protocol only:
    # the paired tool_call/tool_result rows carry the history.
    EXTERNAL_TOOL_CALL = "external_tool_call"
    # Attribution row appended when a session's binding switches, so a
    # shared transcript stays readable across hand-offs. Payload:
    # ``{"from_binding": {...}, "to_binding": {...}, "actor": str,
    #    "binding_epoch": int}``. Display only, never sent to the model.
    AGENT_MARKER = "agent_marker"
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
    # Written by reset_session on ENDED->CREATED re-open. Payload:
    # ``{"invocation": int}``. Rendered by the UI session adapter as a
    # "- invocation N -" divider row (studio-agents-interact §5.2 / §3).
    INVOCATION_DIVIDER = "invocation_divider"
    # Appended (append-only, never a whole-file replace) by the workspace
    # executor's ``_replace_compacted_head`` when history compaction fires.
    # Payload mirrors the chat surface's compaction marker
    # (primer/chat/executor.py): ``{"summary": str, "replaced_from_seq": int,
    # "replaced_to_seq": int, "model": str, "tokens_before": int,
    # "tokens_after": int, "created_at": str}``. The two history readers
    # (``WorkspaceAgentExecutor._read_messages_jsonl`` and
    # ``AgentSession.take_pending_messages``) fold every Message line
    # physically at/before the LAST marker into one synthetic assistant
    # summary; the event log and pre-compaction messages are NEVER deleted.
    COMPACTION_MARKER = "compaction_marker"
    # Structural marker appended by a rewind. Payload: ``{"to_seq": int,
    # "actor": str}``. The read-time replay walk drops every currently
    # visible row with ``seq > to_seq``; nothing is ever deleted, so the
    # append-only invariant above holds for rewinds too. Consumed by the
    # replay rule only, never sent to the model.
    REWIND_MARKER = "rewind_marker"
    # Delivery frame for a NOTIFYING tool call (S3). Payload:
    # ``{"call_id": str, "name": str, "arguments": dict}``. Display and
    # protocol only: the paired tool_call/tool_result rows carry the
    # history, and this record is never sent to the model. Attached
    # clients execute it best-effort off the tap.
    CLIENT_ACTION = "client_action"
    # One record per model call at the shared agent-loop seam
    # (primer/agent/loop.py). Payload: ``{"profile_id": str,
    # "provider_id": str, "model": str, "input_tokens": int,
    # "output_tokens": int, "estimated_input_tokens": int and
    # "context_length": int (only when the provider reported usage),
    # "cached_input_tokens": int (only when it reported them), "guard":
    # "kept" | "reduced" (only when a prompt guard was installed),
    # "duration_ms": int, "status": "ok"}``. Adds
    # per-CALL resolution inside multi-call turns, which the turn log's
    # per-TURN completed event cannot give. Display/derivation only:
    # prompt rebuild never sees it (only role/parts Message lines are
    # history), and transcript renderers hide it - it is Trace-tab
    # material. Carries ``node_id`` when the call ran inside a graph node.
    LLM_CALL = "llm_call"
    # A compaction ran and could not reduce the prompt, or deliberately did
    # nothing, and wrote NO marker (nothing was summarised). Payload:
    # ``{"outcome": "unreducible" | "skipped",
    # "reason": "empty_head" | "fixed_over_budget" | "protected_over_budget"
    # (unreducible) | "cannot_reach_trigger" | "recently_compacted" (skipped),
    # "estimated_tokens":
    # int, "trigger_tokens": int | None}``. A run of skips is noted once (until
    # the next marker), not on every turn. Written through the normal event
    # path (primer/session/persistence.py), so it carries a real seq. A
    # compaction that summarised and was still over the trigger records
    # ``outcome: "insufficient"`` in its marker's payload instead. Display and
    # derivation only: never history, and the transcript renderers hide it.
    COMPACTION_NOTE = "compaction_note"
    # 01a08c08: written by wake_session whenever an operator's pause is
    # touched by an incoming wake, so a superseded pause is never silent.
    # Payload: ``{"action": "cleared" | "queued", "pending_id": str | None}``.
    # "cleared" -- a human-intent wake (console send, channel reply) found
    # pause_requested=True and resumed the session, mirroring an explicit
    # /resume. "queued" -- a non-human wake (trigger fire, agent-to-agent
    # steer, a queued message's own later realization) found the session
    # paused and stored the instruction as a PendingSessionMessage
    # (``pending_id``) instead of clearing the pause -- the pause holds.
    PAUSE_SUPERSEDED = "pause_superseded"
class SessionMessageRecord(BaseModel):
    """One row in the per-session append-only message log.

    One durable record row for the workspace
    session streaming surface.  ``seq`` is monotonically increasing per
    session; the composite ``(session_id, seq)`` is the natural primary
    key (the storage layer composes an ``id`` from these two).
    """

    seq: int = Field(..., ge=1)
    kind: SessionMessageKind
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    node_id: str | None = Field(
        default=None,
        description=(
            "Within-graph node id that produced this record, when the record "
            "originates from a graph run (set by the session translator from "
            "the forwarded ``_GraphNodeEvent`` / graph lifecycle events). "
            "``None`` for plain agent sessions. Lets the tap attribute every "
            "record - including per-node agent tokens/tool calls - to its "
            "originating node so the UI node inspector can stream live."
        ),
    )


class PendingSessionMessage(Identifiable):
    """A follow-up steer received while the session already had a turn.

    Held as its OWN row rather than a list on the session so the enqueue
    (an API process) and the drain (a worker) can never lose-update or
    reorder each other on a last-writer-wins store.

    The row deliberately carries NO seq. Allocating one at receipt is
    what collided with the in-flight turn's assistant_token seqs; the
    drain turns each pending row into a real seq'd ``user_input`` at the
    drain-empty checkpoint, AFTER the active turn's terminal record, so
    the follow-up stays ordered after the response it followed.

    ``id`` shape: ``"{session_id}:pending:{enqueued_at}:{counter}"``. The
    drain orders by ``(enqueued_at, id)``.
    """

    session_id: str = Field(..., min_length=1)
    parts: list[dict[str, Any]] = Field(default_factory=list)
    attribution: dict[str, Any] | None = Field(default=None)
    client_msg_id: str | None = Field(default=None)
    enqueued_at: datetime = Field(...)
    created_at: datetime = Field(...)


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
    "NON_ENDED_STATUSES",
    "NON_ENDED_STATUSES_NOT_PAUSED",
    "SessionMessageKind",
    "SessionMessageRecord",
    "WorkspaceSession",
    "SessionBinding",
    "SessionInfo",
    "SessionStatus",
    "WaitingState",
]
