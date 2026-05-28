"""Yielding-tool protocol primitives.

Spec: ``docs/superpowers/specs/2026-05-22-yielding-tools-design.md``.

A *yielding tool* suspends the calling agent's turn until an external
event fires. The agent's worker writes the in-progress turn state to
the sessions table, releases its lease, and goes back to claiming
other sessions. When the event fires, any worker resumes the turn —
the tool's :meth:`resume` hook receives the event payload, returns a
tool result, and the LLM call continues.

This module ships the M1 foundation:

* :class:`Yielded` — the sentinel a yielding tool returns instead of
  a normal :class:`primer.model.chat.ToolCallResult`.
* :class:`YieldTimeout` — synthetic payload passed to a tool's
  :meth:`resume` hook when the park's ``parked_until`` elapses
  before any real event fires.
* :class:`YieldCancelled` — synthetic payload passed to a tool's
  :meth:`resume` hook when an operator cancels the *yield* (not the
  session). The agent's turn continues with a cancelled-tool result.
* :class:`ToolContext` — injected per-call context giving the tool
  its own ``tool_call_id``, ``session_id``, and (on resume) the
  ``parked_at`` timestamp.
* :class:`YieldToWorker` — internal control-flow exception the tool
  engine raises when it sees a :class:`Yielded` return. Bubbles up
  through the LLM loop to the worker's park path.

The dataclasses use ``frozen=True`` so they're hashable and safe to
serialise/deserialise across the park boundary without copy
surprises. JSONB-friendly: every field is either a primitive,
``datetime``, or a nested dict of primitives.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


# ===========================================================================
# Sentinels returned by yielding tools
# ===========================================================================


@dataclass(frozen=True)
class Yielded:
    """The sentinel a yielding tool returns instead of a normal result.

    Returning a ``Yielded`` from a tool handler tells the tool engine
    to park the calling session and release the worker's lease until
    the event identified by ``event_key`` fires (or ``timeout``
    elapses, whichever comes first).

    Attributes
    ----------
    tool_name
        Name of the tool that returned this ``Yielded``. Stamped by
        the tool engine into the parked-state blob so the resume
        path can look up the tool's ``resume`` hook directly from
        the blob, without rehydrating the LLM message history first.
        Tools typically don't set this themselves — the engine fills
        it from the tool's registered name at park time.
    event_key
        Routing key for the event bus. Conventional prefixes
        documented in spec §3:

        * ``timer:{tool_call_id}`` — wakes from the timer scheduler.
        * ``ask_user:{session_id}:{tool_call_id}`` — wakes from
          the ``POST /v1/sessions/{id}/ask_user/respond`` endpoint.
        * ``watch:{session_id}:{tool_call_id}`` — wakes from the
          local filesystem watcher.
        * ``mcp_task:{server_id}:{task_id}`` — wakes when the MCP
          server signals task completion.
    timeout
        Seconds to wait before auto-resuming with a
        :class:`YieldTimeout` payload. ``None`` means use the
        global yield-timeout cap (default 60 minutes, configurable).
    resume_metadata
        Opaque blob the tool's :meth:`resume` hook receives back
        when the event fires. Use it to carry the original
        arguments through so :meth:`resume` can synthesise the right
        tool result. Must be JSON-serialisable.
    """

    tool_name: str
    event_key: str
    timeout: float | None = None
    resume_metadata: dict[str, Any] = field(default_factory=dict)

    def to_jsonable(self) -> dict[str, Any]:
        """Serialise for storage in the parked-state blob."""
        return {
            "tool_name": self.tool_name,
            "event_key": self.event_key,
            "timeout": self.timeout,
            "resume_metadata": dict(self.resume_metadata),
        }

    @classmethod
    def from_jsonable(cls, data: dict[str, Any]) -> "Yielded":
        return cls(
            tool_name=data["tool_name"],
            event_key=data["event_key"],
            timeout=data.get("timeout"),
            resume_metadata=dict(data.get("resume_metadata") or {}),
        )


# ===========================================================================
# Synthetic resume payloads
# ===========================================================================


@dataclass(frozen=True)
class YieldTimeout:
    """Resume payload synthesised when a park hits its deadline.

    The resume path constructs this when the session's
    ``parked_until`` elapsed before the real event fired. The tool's
    :meth:`resume` hook sees this in place of the normal event
    payload and surfaces it as a tool result the agent can react to
    (typically ``{"timed_out": true, "elapsed_seconds": ...}``).
    """

    elapsed_seconds: float


@dataclass(frozen=True)
class YieldCancelled:
    """Resume payload synthesised when an operator cancels the yield.

    Distinct from cancel-session: cancel-session terminates the
    whole session (the tool's :meth:`resume` is never called).
    Cancel-yielded-tool only cancels the in-flight yield; the
    tool's :meth:`resume` returns a "cancelled" result and the
    agent's turn continues normally.

    Attributes
    ----------
    reason
        Operator-supplied reason string (or ``None``). Surfaced
        verbatim in the tool result so the agent can reflect on
        why the user cancelled.
    cancelled_at
        Timestamp the cancel signal was published.
    elapsed_seconds
        Time the park was alive before cancel — useful for the
        tool result so the agent has context on how long it waited.
    """

    reason: str | None
    cancelled_at: datetime
    elapsed_seconds: float


# ===========================================================================
# Per-call context injected into yielding tools
# ===========================================================================


@dataclass(frozen=True)
class ToolContext:
    """Per-call context the tool engine injects into yielding tools.

    Tools that yield need their own ``tool_call_id`` (to form unique
    event keys), their session id (to scope event keys), and on
    resume the ``parked_at`` timestamp (to compute elapsed time).

    Tools that don't yield never receive this — the engine inspects
    each handler's signature and only injects when the parameter is
    declared.

    Attributes
    ----------
    tool_call_id
        Unique id for this specific tool invocation, allocated by
        the LLM adapter. Stable across park and resume.
    session_id
        Owning session's id. ``None`` for chat-only invocations
        (M6 — chat sessions get their own id via a separate
        :class:`ChatContext` once that milestone lands).
    workspace_id
        Workspace id when the session is workspace-bound; ``None``
        for chat-only invocations.
    parked_at
        Set on resume to the timestamp the park was originally
        written; ``None`` on the initial call. Tools use this to
        compute how long they were parked for.
    """

    tool_call_id: str
    session_id: str | None
    workspace_id: str | None
    parked_at: datetime | None = None


# ===========================================================================
# Control-flow exception (internal — escapes the LLM loop)
# ===========================================================================


class YieldToWorker(Exception):
    """Raised by the tool engine when it sees a :class:`Yielded`.

    Bubbles up through the LLM call loop and the executor's
    ``invoke()`` to the worker's ``_run_one_turn``. The worker
    catches this, parks the session in storage, and releases its
    lease. The exception is NOT user-facing — agents never observe
    it; their tool call simply returns whatever :meth:`resume`
    produces when the event eventually fires.

    Carries the :class:`Yielded` sentinel plus enough context for
    the worker to construct the parked-state blob.

    ``llm_messages`` holds the in-progress turn's assistant +
    tool-result messages — populated by the executor's ``invoke``
    just before re-raising, so the worker's park hook can persist
    them into :class:`ParkedState`. Load-bearing for the resume
    path: the assistant message that emitted the tool_use is
    accumulated in the executor's frame BUT not yet
    ``_persist_turn``'d when the yield fires (persistence happens
    at end-of-stream). Without this field the resume path would
    have no preceding tool_use to pair the synthesised tool_result
    against, and the LLM history would be malformed.

    Tools that raise YieldToWorker themselves (e.g. the approval
    gate in :mod:`primer.agent.tool_manager`) do NOT need to
    populate this — the executor stamps it on the way out.
    """

    def __init__(
        self,
        yielded: Yielded,
        *,
        tool_call_id: str,
        llm_messages: list | None = None,
    ) -> None:
        super().__init__(
            f"tool {yielded.tool_name!r} yielded; "
            f"event_key={yielded.event_key!r} "
            f"tool_call_id={tool_call_id!r}"
        )
        self.yielded = yielded
        self.tool_call_id = tool_call_id
        # The executor stamps in-progress turn messages here on the
        # way out (primer/agent/base.py). Default ``None`` lets
        # callers that raise directly leave it for the executor to
        # fill in.
        self.llm_messages: list | None = llm_messages


__all__ = [
    "Yielded",
    "YieldTimeout",
    "YieldCancelled",
    "ToolContext",
    "YieldToWorker",
]
