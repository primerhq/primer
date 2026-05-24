"""Worker-side helpers for the yielding-tool park / resume flow.

Spec: ``docs/superpowers/specs/2026-05-22-yielding-tools-design.md``
§5 (DB shape), §7 (worker semantics).

This module is the bridge between the pure-protocol primitives in
:mod:`matrix.model.yield_` and the worker pool's turn-execution loop.
It owns three responsibilities:

* **Park-state serialisation** — package a turn's mid-flight LLM
  message history + the Yielded sentinel + turn metadata into the
  JSONB blob the sessions row stores under ``parked_state``.
* **Resume-state rehydration** — rebuild the turn-in-progress from
  a serialised blob, decide what resume payload to feed to the
  tool's :meth:`resume` hook (real event payload vs ``YieldTimeout``
  vs ``YieldCancelled``), and return a structured object the worker
  can act on.
* **Resume-payload classification** — the bus delivers a generic
  ``dict`` payload alongside two sentinel markers
  (``__yield_timeout__``, ``__yield_cancelled__``); this module
  detects the markers and synthesises the right Python-side
  :class:`YieldTimeout` / :class:`YieldCancelled` instances.

None of this module touches the DB directly — callers (the worker
pool and the session API endpoints) thread the parked Session row
through. Keeping I/O at the edges makes the helpers fast to unit
test and easy to reason about.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from matrix.model.chat import ToolCallPart, ToolResultPart
from matrix.model.yield_ import (
    YieldCancelled,
    YieldTimeout,
    Yielded,
)


# Schema version for the parked_state blob. Bumped only on
# backwards-incompatible shape changes. The rehydrate helper rejects
# unknown versions loudly so a future runtime can't silently misread
# old parks.
PARKED_STATE_SCHEMA_VERSION = 1


# Marker keys placed inside resume_event_payload by the timeout
# sweeper / cancel-yielded-tool API. The runtime detects these and
# routes to the matching synthetic resume payload.
_YIELD_TIMEOUT_KEY = "__yield_timeout__"
_YIELD_CANCELLED_KEY = "__yield_cancelled__"


# ===========================================================================
# Serialisation
# ===========================================================================


@dataclass(frozen=True)
class ParkedState:
    """Structured view of the JSONB blob stored under sessions.parked_state.

    Constructed at park time from the Yielded sentinel + the
    in-progress turn's message history. Round-trips losslessly
    through JSON via :meth:`to_jsonable` / :meth:`from_jsonable`.

    Attributes
    ----------
    yielded
        The :class:`Yielded` instance the tool returned.
    llm_messages
        LLM message history up to and including the assistant
        message that emitted the yielding tool call. Canonical
        Matrix message-dict format (per-LLM-family translation
        happens on rehydration).
    turn_no
        Turn number this park was made in. The same turn-no is
        used on resume (the resumed turn is a continuation of the
        parked one, not a new turn).
    started_at
        Timestamp the original turn began. Carried through so
        post-resume reporting (e.g. session.last_turn_at) reflects
        the true latency.
    resume_event_payload
        Set by the publisher of the resume event (the event bus
        listener or the cancel-yielded-tool API). ``None`` until
        the park has been flipped to resumable. Marker keys inside
        this dict trigger the synthetic-payload paths (see
        :func:`classify_resume_payload`).
    """

    yielded: Yielded
    llm_messages: list[dict[str, Any]]
    turn_no: int
    started_at: datetime
    tool_call_id: str | None = None
    resume_event_payload: dict[str, Any] | None = None
    schema_version: int = PARKED_STATE_SCHEMA_VERSION

    def to_jsonable(self) -> dict[str, Any]:
        """Render to a JSON-safe dict for persistence in sessions.data."""
        return {
            "schema_version": self.schema_version,
            "tool_call_id": self.tool_call_id,
            "yielded": self.yielded.to_jsonable(),
            "llm_messages": list(self.llm_messages),
            "turn_no": self.turn_no,
            "started_at": self.started_at.isoformat(),
            "resume_event_payload": (
                dict(self.resume_event_payload)
                if self.resume_event_payload is not None
                else None
            ),
        }

    @classmethod
    def from_jsonable(cls, data: dict[str, Any]) -> "ParkedState":
        version = data.get("schema_version", PARKED_STATE_SCHEMA_VERSION)
        if version != PARKED_STATE_SCHEMA_VERSION:
            # Loud fail — better than silently mis-rehydrating an
            # old park whose layout doesn't match what we expect.
            raise ValueError(
                f"unknown parked_state schema_version {version!r}; "
                f"this runtime understands {PARKED_STATE_SCHEMA_VERSION}"
            )
        return cls(
            yielded=Yielded.from_jsonable(data["yielded"]),
            llm_messages=list(data["llm_messages"]),
            turn_no=int(data["turn_no"]),
            started_at=_parse_iso(data["started_at"]),
            tool_call_id=data.get("tool_call_id"),
            resume_event_payload=(
                dict(data["resume_event_payload"])
                if data.get("resume_event_payload") is not None
                else None
            ),
            schema_version=version,
        )


# ===========================================================================
# Resume payload classification
# ===========================================================================


@dataclass(frozen=True)
class ResumePayload:
    """What :func:`classify_resume_payload` produces.

    Wraps one of three Python-side payloads the tool's
    :meth:`resume` hook will receive:

    * a real event dict (the external source's payload),
    * :class:`YieldTimeout`,
    * :class:`YieldCancelled`.

    Plus the elapsed-seconds since park, which several tools want
    to surface in their tool result.
    """

    payload: dict[str, Any] | YieldTimeout | YieldCancelled
    elapsed_seconds: float


def classify_resume_payload(
    parked_state: ParkedState,
    *,
    parked_at: datetime,
    now: datetime | None = None,
) -> ResumePayload:
    """Decide which Python-side payload to feed the tool's resume hook.

    Inspects ``parked_state.resume_event_payload`` for the two
    documented marker keys; if neither is present, treats the dict
    as a real event payload.

    Parameters
    ----------
    parked_state
        The rehydrated state. Must have ``resume_event_payload`` set
        (caller is responsible for not invoking this function until
        the park is resumable).
    parked_at
        When the park was originally written — drives
        ``elapsed_seconds`` calculation and the
        ``YieldCancelled.elapsed_seconds`` field. Passed in
        explicitly (rather than read from ``parked_state``) because
        callers already have it as a typed Session field.
    now
        Override for "current time" — useful for deterministic
        tests. Defaults to ``datetime.now(timezone.utc)``.

    Returns
    -------
    ResumePayload
        Structured wrapper carrying the Python-side payload plus the
        computed ``elapsed_seconds``.

    Raises
    ------
    ValueError
        If ``resume_event_payload`` is ``None`` (the caller invoked
        this before the park became resumable).
    """
    if parked_state.resume_event_payload is None:
        raise ValueError(
            "classify_resume_payload called before resume_event_payload "
            "was set — the park is still in 'parked' state, not "
            "'resumable'"
        )

    current = now or datetime.now(timezone.utc)
    elapsed = (current - parked_at).total_seconds()
    raw = parked_state.resume_event_payload

    if _YIELD_TIMEOUT_KEY in raw:
        return ResumePayload(
            payload=YieldTimeout(elapsed_seconds=elapsed),
            elapsed_seconds=elapsed,
        )

    if _YIELD_CANCELLED_KEY in raw:
        cancelled_at_iso = raw.get("cancelled_at")
        cancelled_at = (
            _parse_iso(cancelled_at_iso)
            if cancelled_at_iso
            else current
        )
        return ResumePayload(
            payload=YieldCancelled(
                reason=raw.get("reason"),
                cancelled_at=cancelled_at,
                elapsed_seconds=elapsed,
            ),
            elapsed_seconds=elapsed,
        )

    # Real event payload — strip the matrix-internal control keys if
    # the publisher happened to include them, then pass through.
    payload = {
        k: v for k, v in raw.items()
        if not k.startswith("__yield_") and k != "cancelled_at"
    }
    return ResumePayload(payload=payload, elapsed_seconds=elapsed)


# ===========================================================================
# Publishers' payload helpers
# ===========================================================================


def make_timeout_payload() -> dict[str, Any]:
    """Build the marker payload the timeout sweeper publishes.

    Tiny convenience so the sweeper doesn't have to know the marker
    key by string.
    """
    return {_YIELD_TIMEOUT_KEY: True}


def make_cancelled_payload(
    *,
    reason: str | None,
    cancelled_at: datetime | None = None,
) -> dict[str, Any]:
    """Build the marker payload the cancel-yielded-tool API publishes."""
    at = cancelled_at or datetime.now(timezone.utc)
    return {
        _YIELD_CANCELLED_KEY: True,
        "reason": reason,
        "cancelled_at": at.isoformat(),
    }


# ===========================================================================
# Internals
# ===========================================================================


def _parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp, defaulting to UTC if no tz.

    Postgres' ``timestamptz`` round-trips as a tz-aware ISO string,
    so the fallback is defensive (e.g. for test fixtures).
    """
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ===========================================================================
# Tool-approval resume path
# ===========================================================================


async def _resume_tool_approval(
    *,
    blob: dict,
    payload: "dict | YieldTimeout | YieldCancelled | Any",
    tool_manager: Any,
) -> ToolResultPart:
    """Resume a session parked on a tool-approval gate.

    Classifies the event payload and either re-dispatches the
    original tool call (decision=approved) or synthesises an error
    ToolResultPart (decision=rejected, timeout, cancelled, or any
    malformed payload — fail-closed).

    Parameters
    ----------
    blob
        The raw parked-state JSON blob (the ``"yielded"`` key carries
        the Yielded sentinel including ``resume_metadata``).
    payload
        The classified resume payload — either a real event dict
        (with ``"decision": "approved"`` or ``"decision": "rejected"``),
        a :class:`YieldTimeout`, or a :class:`YieldCancelled`.
        Any other type is treated as malformed and synthesises a
        rejection.
    tool_manager
        A :class:`~matrix.agent.tool_manager.ToolExecutionManager`
        (or compatible duck-type) that exposes
        ``execute(call, *, bypass_approval=True) -> ToolResultPart``.
        Only called on the approved path.

    Returns
    -------
    ToolResultPart
        Either the real tool result (approved) or a synthetic error
        result (rejected / timeout / cancelled / malformed).
    """
    metadata = (blob.get("yielded") or {}).get("resume_metadata") or {}
    original_raw = metadata.get("original_call") or {}
    original_call = ToolCallPart(
        id=original_raw.get("id", "unknown"),
        name=original_raw.get("name", "unknown"),
        arguments=original_raw.get("arguments") or {},
    )

    decision: str
    reason: str | None
    if isinstance(payload, YieldTimeout):
        decision = "rejected"
        reason = "timed-out"
    elif isinstance(payload, YieldCancelled):
        decision = "rejected"
        reason = payload.reason or "cancelled"
    elif isinstance(payload, dict):
        raw_decision = payload.get("decision")
        if raw_decision == "approved":
            decision = "approved"
            reason = payload.get("reason")
        elif raw_decision == "rejected":
            decision = "rejected"
            reason = payload.get("reason")
        else:
            decision = "rejected"
            reason = "malformed approval payload (missing decision)"
    else:
        decision = "rejected"
        reason = "malformed approval payload (non-dict)"

    if decision == "approved":
        return await tool_manager.execute(original_call, bypass_approval=True)

    return ToolResultPart(
        id=original_call.id,
        output=json.dumps({
            "rejected": True,
            "reason": reason or "(no reason supplied)",
            "tool_name": original_call.name,
            "arguments": original_call.arguments,
        }),
        error=True,
    )


__all__ = [
    "ParkedState",
    "PARKED_STATE_SCHEMA_VERSION",
    "ResumePayload",
    "_resume_tool_approval",
    "classify_resume_payload",
    "make_cancelled_payload",
    "make_timeout_payload",
]
