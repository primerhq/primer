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
from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field


# ===========================================================================
# Session status
# ===========================================================================


class SessionStatus(str, Enum):
    """Lifecycle state of an :class:`AgentSession`.

    Transitions are driven by the agent runtime (which sets
    :attr:`RUNNING` while a turn is in flight, :attr:`WAITING` when
    blocked on either user input or an approval, :attr:`ENDED` on
    terminal) and by the user (who can request :attr:`PAUSED` and
    resume back to :attr:`RUNNING`).

    :attr:`WAITING` is intentionally one state regardless of what the
    session is blocked on -- the distinction is recorded in
    ``.state/sessions/<id>/waiting.json`` via the :class:`WaitingState`
    discriminated union, which the user inspects to figure out what
    response is needed.

    Terminal: :attr:`ENDED`. Non-terminal: everything else.
    """

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
    Union[_UserInputWaiting, _ToolApprovalWaiting],
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

    What :meth:`matrix.int.Workspace.list_sessions` returns. Persisted
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
    ended_reason: Literal["completed", "failed", "cancelled"] | None = Field(
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
# Re-exports
# ===========================================================================


__all__ = [
    "AgentBinding",
    "Instruction",
    "SessionInfo",
    "SessionStatus",
    "WaitingState",
]
