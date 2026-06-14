"""Worker-side helpers for the yielding-tool park / resume flow.

Spec: ``docs/superpowers/specs/2026-05-22-yielding-tools-design.md``
§5 (DB shape), §7 (worker semantics).

This module is the bridge between the pure-protocol primitives in
:mod:`primer.model.yield_` and the worker pool's turn-execution loop.
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

from primer.model.chat import ToolCallPart, ToolResultPart
from primer.model.yield_ import (
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
        Primer message-dict format (per-LLM-family translation
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
    # Spec B Phase 6/11 — set when a graph-bound session parks at a
    # ToolCall approval gate. Carries the JSON-able payload produced by
    # :meth:`Graph.snapshot_state` so any worker can rehydrate the
    # mid-flight graph executor on resume. ``None`` for agent-bound
    # parks (their continuation runs through the LLM history alone).
    graph_checkpoint: dict[str, Any] | None = None
    # Unified nested-yield continuation stack: an ordered (root-first) list
    # of AgentFrame/GraphFrame instances capturing every in-flight caller
    # above the leaf yield. Empty for a flat (single-frame) park written by
    # an older runtime; ``from_jsonable`` synthesises a one-frame stack in
    # that case so downstream resume code always sees at least one frame.
    frames: list = field(default_factory=list)
    schema_version: int = PARKED_STATE_SCHEMA_VERSION

    def to_jsonable(self) -> dict[str, Any]:
        """Render to a JSON-safe dict for persistence in sessions.data."""
        # Lazy import: frames.py imports from this module, so a top-level
        # import would create a cycle.
        from primer.worker.frames import frames_to_jsonable
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
            "graph_checkpoint": (
                dict(self.graph_checkpoint)
                if self.graph_checkpoint is not None
                else None
            ),
            "frames": frames_to_jsonable(self.frames),
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
        missing = {"yielded", "llm_messages", "turn_no", "started_at"} - set(data)
        if missing:
            # A corrupt / truncated blob: a clear ValueError beats a bare
            # KeyError so the resume path's guard logs an actionable reason.
            raise ValueError(
                f"parked_state blob missing required keys: {sorted(missing)}"
            )
        # Lazy import: frames.py imports from this module (cycle avoidance).
        from primer.worker.frames import (
            AgentFrame,
            AgentResumeContext,
            GraphFrame,
            frames_from_jsonable,
        )

        yielded = Yielded.from_jsonable(data["yielded"])
        llm_messages = list(data["llm_messages"])
        tool_call_id = data.get("tool_call_id")
        graph_checkpoint = (
            dict(data["graph_checkpoint"])
            if data.get("graph_checkpoint") is not None
            else None
        )

        raw_frames = data.get("frames")
        if raw_frames:
            frames = frames_from_jsonable(raw_frames)
        else:
            # Back-compat shim: an OLD park (and every pre-unification
            # invoke_graph park) has no ``frames`` key. Reconstruct a
            # single-frame continuation stack so resume code can treat all
            # parks uniformly. The leaf stays ``yielded``.
            md = yielded.resume_metadata or {}
            if yielded.tool_name == "invoke_graph" or graph_checkpoint is not None:
                frames = [
                    GraphFrame(
                        graph_id=md.get("graph_id"),
                        gsid=md.get("sub_gsid"),
                        checkpoint=graph_checkpoint,
                        tool_call_id=md.get("child_tcid") or tool_call_id,
                    )
                ]
            else:
                frames = [
                    AgentFrame(
                        agent_id=None,
                        llm_messages=llm_messages,
                        tool_call_id=tool_call_id,
                        depth=0,
                        context=AgentResumeContext(
                            session_id=None,
                            workspace_id=None,
                            chat_id=None,
                            principal=None,
                            tools=[],
                        ),
                    )
                ]

        return cls(
            yielded=yielded,
            llm_messages=llm_messages,
            turn_no=int(data["turn_no"]),
            started_at=_parse_iso(data["started_at"]),
            tool_call_id=tool_call_id,
            resume_event_payload=(
                dict(data["resume_event_payload"])
                if data.get("resume_event_payload") is not None
                else None
            ),
            graph_checkpoint=graph_checkpoint,
            frames=frames,
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

    # Real event payload — strip the primer-internal control keys if
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


def classify_approval_payload(
    payload: "dict[str, Any] | YieldTimeout | YieldCancelled | Any",
) -> tuple[str, str | None]:
    """Classify a resume payload into ``(decision, reason)``.

    The single source of truth for how an approval park interprets its
    resume payload: a timeout or cancellation becomes a rejection with a
    canned reason, an explicit ``{"decision": ...}`` dict is honoured, and
    anything malformed fails closed to a rejection. Both the agent-session
    resume (:func:`_resume_tool_approval`) and the graph resume adapter
    (:func:`primer.worker.graph_resume._decision_from_payload`) call this so
    the two paths cannot drift.
    """
    if isinstance(payload, YieldTimeout):
        return "rejected", "timed-out"
    if isinstance(payload, YieldCancelled):
        return "rejected", payload.reason or "cancelled"
    if isinstance(payload, dict):
        raw = payload.get("decision")
        reason = payload.get("reason")
        if raw == "approved":
            return "approved", reason
        if raw == "rejected":
            return "rejected", reason
        return "rejected", "malformed approval payload (missing decision)"
    return "rejected", "malformed approval payload (non-dict)"


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
        A :class:`~primer.agent.tool_manager.ToolExecutionManager`
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

    decision, reason = classify_approval_payload(payload)

    if decision == "approved":
        # ``system__call_tool`` parks the INNER (toolset_id, tool_name) it
        # was asked to meta-dispatch, which is not necessarily part of the
        # agent's registered tool surface - so it cannot be re-routed
        # through ``tool_manager.execute``. Re-dispatch it directly via the
        # owning toolset provider, mirroring how the call_tool handler does
        # it. ``via_call_tool`` carries the inner toolset id + principal.
        via = metadata.get("via_call_tool")
        if via is not None:
            return await _resume_call_tool_dispatch(
                via=via,
                original_call=original_call,
                tool_manager=tool_manager,
            )
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


async def _resume_call_tool_dispatch(
    *,
    via: dict[str, Any],
    original_call: ToolCallPart,
    tool_manager: Any,
) -> ToolResultPart:
    """Dispatch an approved ``call_tool`` inner tool via its toolset provider.

    The inner tool was gated through ``system__call_tool``; on approval we
    invoke it the same way the call_tool handler would (resolve the owning
    toolset provider from the registry, then ``provider.call(...)``) rather
    than routing it through the agent's tool surface (which may not list it).
    Fails closed to an error ToolResultPart if the registry/provider is
    unavailable.
    """
    registry = getattr(tool_manager, "_provider_registry", None)
    toolset_id = via.get("toolset_id")
    if registry is None or not toolset_id:
        return ToolResultPart(
            id=original_call.id,
            output=json.dumps({
                "is_error": True,
                "reason": "call_tool resume: provider registry unavailable",
                "tool_name": original_call.name,
            }),
            error=True,
        )
    try:
        provider = await registry.get_toolset(toolset_id)
        result = await provider.call(
            tool_name=original_call.name,
            arguments=original_call.arguments,
            principal=via.get("principal"),
        )
    except Exception as exc:  # noqa: BLE001 - fail-closed synthesis
        return ToolResultPart(
            id=original_call.id,
            output=json.dumps({
                "is_error": True,
                "reason": f"call_tool resume failed: {type(exc).__name__}: {exc}",
                "tool_name": original_call.name,
            }),
            error=True,
        )
    return ToolResultPart(
        id=original_call.id,
        output=result.output,
        error=result.is_error,
    )


# ===========================================================================
# Post-park channel dispatch
# ===========================================================================


from primer.channel.adapter import PromptEnvelope


def _build_prompt_envelope(
    *,
    kind: str,
    workspace_id: str,
    session_id: str,
    fallback_tool_call_id: str,
    metadata: dict[str, Any],
    workspace_name: str | None = None,
    session_label: str | None = None,
) -> "PromptEnvelope | None":
    """Build the channel PromptEnvelope for one parked human-interaction.

    ``kind`` is the yielding tool name: ``ask_user`` (free-text question)
    or ``_approval`` (tool-approval gate). Returns ``None`` for any other
    tool (e.g. sleep/watch_files), which are not forwarded to channels.
    Shared by the single- and multi-event dispatch paths so the envelope
    mapping lives in exactly one place.
    """
    metadata = metadata or {}
    if kind == "ask_user":
        return PromptEnvelope(
            kind="ask_user",
            workspace_id=workspace_id,
            session_id=session_id,
            tool_call_id=fallback_tool_call_id,
            prompt=metadata.get("prompt", ""),
            response_schema=metadata.get("response_schema"),
            choices=None,
            timeout_at_iso=None,
            workspace_name=workspace_name,
            session_label=session_label,
        )
    if kind == "_approval":
        original = metadata.get("original_call") or {}
        gate_reason = metadata.get("gate_reason")
        prompt = (
            f"Approve {original.get('name', '<unknown>')}"
            f"({original.get('arguments') or {}})?"
        )
        if gate_reason:
            prompt += f"\nReason: {gate_reason}"
        return PromptEnvelope(
            kind="tool_approval",
            workspace_id=workspace_id,
            session_id=session_id,
            tool_call_id=original.get("id") or fallback_tool_call_id,
            prompt=prompt,
            response_schema=None,
            choices=["Approve", "Reject"],
            timeout_at_iso=None,
            tool_name=original.get("name"),
            tool_args=original.get("arguments") or {},
            workspace_name=workspace_name,
            session_label=session_label,
        )
    return None


async def _resolve_files_to_media(
    *, workspace_registry, artifact_registry, workspace_id, files,
) -> "list[dict] | None":
    """Read ask_user/inform workspace files into artifact-backed media part
    dicts for a PromptEnvelope. None when files/registries are missing."""
    import logging
    if not files or workspace_registry is None or artifact_registry is None:
        return None
    from primer.channel.media import media_from_workspace_files
    try:
        workspace = await workspace_registry.get_workspace(workspace_id)
        store = await artifact_registry.get_default()
    except Exception:
        logging.getLogger(__name__).warning(
            "ask_user files: workspace/artifact resolve failed")
        return None
    parts = await media_from_workspace_files(workspace, store, files)
    return [p.model_dump(mode="json") for p in parts] or None


async def _dispatch_to_channels(
    *,
    dispatcher,
    session,
    yielded,
    workspace_registry=None,
    artifact_registry=None,
    workspace_name: str | None = None,
    session_label: str | None = None,
) -> None:
    """Fan a parked-on-user-input session out to every channel
    associated with the session's workspace.

    Fire-and-forget at the call site (the worker schedules this
    on the event loop). Internal errors are logged; this function
    never raises.
    """
    import logging
    if dispatcher is None:
        return
    metadata = yielded.resume_metadata or {}
    envelope = _build_prompt_envelope(
        kind=yielded.tool_name,
        workspace_id=session.workspace_id,
        session_id=session.id,
        fallback_tool_call_id=_tool_call_id_from_event_key(yielded.event_key),
        metadata=metadata,
        workspace_name=workspace_name,
        session_label=session_label,
    )
    if envelope is None:
        return
    # Attach any ask_user `files` as media (read from the workspace + stored).
    if envelope.kind == "ask_user" and metadata.get("files"):
        envelope.media = await _resolve_files_to_media(
            workspace_registry=workspace_registry,
            artifact_registry=artifact_registry,
            workspace_id=session.workspace_id,
            files=metadata.get("files"),
        )
    try:
        await dispatcher.dispatch_prompt(envelope=envelope)
    except Exception:
        logging.getLogger(__name__).exception(
            "_dispatch_to_channels failed for session %s", session.id,
        )


def merge_pending_dispatch(checkpoint: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the full per-node channel-dispatch list for a parked graph.

    The checkpoint persists ``pending_dispatch`` only for tool-call nodes:
    those entries bake the graph node's ``tool_id`` into ``original_call``,
    which the channel layer can't recompute without the graph. Agent yields
    are derived here from ``pending_agent_yields`` so their resume_metadata
    is stored once, not duplicated into ``pending_dispatch`` as well.

    Backward compatible: a pre-slim checkpoint that still carries agent-yield
    entries inside ``pending_dispatch`` produces a duplicate here, but the
    channel dispatcher dedups by ``tool_call_id`` so the prompt is sent once.
    """
    stored = list(checkpoint.get("pending_dispatch") or [])
    derived = [
        {
            "kind": ay.get("tool_name", ""),
            "tool_call_id": ay.get("tool_call_id"),
            "resume_metadata": dict(ay.get("resume_metadata") or {}),
        }
        for ay in (checkpoint.get("pending_agent_yields") or [])
    ]
    return stored + derived


async def _dispatch_to_channels_multi(
    *,
    dispatcher,
    workspace_id: str,
    session_id: str,
    pending: list[dict[str, Any]],
    already_sent: set[str],
    workspace_name: str | None = None,
    session_label: str | None = None,
) -> set[str]:
    """Send one channel prompt per pending human-interaction node.

    For a multi-event graph park (several agent/tool_call nodes yielding
    in one superstep), each pending node gets its own message. ``pending``
    items are ``{"kind": "ask_user"|"_approval", "tool_call_id",
    "resume_metadata"}``. Returns the union of ``already_sent`` and the
    tool_call_ids dispatched this call, so a re-park does not re-send a
    message for a node already prompted. Never raises.
    """
    import logging
    if dispatcher is None:
        return set(already_sent)
    sent = set(already_sent)
    for p in pending:
        tcid = p.get("tool_call_id")
        if not tcid or tcid in sent:
            continue
        envelope = _build_prompt_envelope(
            kind=p.get("kind", ""),
            workspace_id=workspace_id,
            session_id=session_id,
            fallback_tool_call_id=tcid,
            metadata=p.get("resume_metadata") or {},
            workspace_name=workspace_name,
            session_label=session_label,
        )
        if envelope is None:
            continue
        try:
            await dispatcher.dispatch_prompt(envelope=envelope)
            sent.add(tcid)
        except Exception:
            logging.getLogger(__name__).exception(
                "_dispatch_to_channels_multi failed for %s/%s",
                session_id, tcid,
            )
    return sent


def _tool_call_id_from_event_key(event_key: str) -> str:
    """Pull the tool_call_id segment out of an event_key."""
    parts = event_key.split(":")
    return parts[-1] if len(parts) >= 3 else ""


__all__ = [
    "ParkedState",
    "PARKED_STATE_SCHEMA_VERSION",
    "ResumePayload",
    "_dispatch_to_channels",
    "_dispatch_to_channels_multi",
    "_resume_tool_approval",
    "_tool_call_id_from_event_key",
    "classify_resume_payload",
    "make_cancelled_payload",
    "make_timeout_payload",
    "merge_pending_dispatch",
]
