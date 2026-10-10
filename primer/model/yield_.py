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
``datetime``, or a nested dict of primitives. (``ToolContext.inform``
is the lone exception — a transient runtime sink that is never
persisted or checkpointed.)
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from primer.model.principal import PrincipalRef


if TYPE_CHECKING:
    from primer.model.chat import ToolResultPart


# ===========================================================================
# The identity of one human gate
# ===========================================================================

GATE_ID_KEY = "gate_id"
"""Where a gate's id lives: ``resume_metadata["gate_id"]`` of its pending entry (an approval park or an ``ask_user`` park)."""

GATE_ID_PATTERN = r"^[0-9a-f]{32}$"
"""What a gate id looks like on the wire (the respond bodies refuse anything else with a 422)."""


def new_gate_id() -> str:
    """Mint the id of a gate that is being created.

    A provider repeats its ``tool_call_id`` across rounds, and the approval / ask_user event keys are built from it, so neither identifies WHICH
    gate a decision answers. The id is minted once, when the gate is created, and rides in the pending entry's ``resume_metadata``, which the
    graph checkpoint round-trips verbatim: it survives every re-park for as long as the gate stays pending, and a later gate under the same raw
    id gets another. (The session's ``parked_at`` cannot stand in for it: answering one gate of a multi-gate graph park re-stamps it while the
    siblings are still pending.)
    """
    return uuid.uuid4().hex


def gate_id_of(resume_metadata: "dict[str, Any] | None") -> str | None:
    """The gate id stamped in a pending entry's ``resume_metadata``; ``None`` for a park written before gates had one."""
    value = (resume_metadata or {}).get(GATE_ID_KEY)
    return value if isinstance(value, str) and value else None


WAKE_GATE_ID_KEY = "__yield_gate_id__"
"""Where a human decision's wake payload names the gate it decided (C-033 round 2, PR 4).

The wake of a decision is delivered by event key alone and at least once, so one redelivered after the session re-parked under the same provider id (the same
event key, a new gate) used to flip the NEW gate with the old decision. The key has the primer-internal ``__yield_`` prefix, so the resume classification
strips it from a real reply and no hook or approval classifier ever sees it."""


WAKE_PARK_KEY = "__yield_parked_at__"
"""Where a wake published by a producer that READ the park names the park it read (its ``parked_at``, ISO 8601). The producers that stamp it: a trigger fire
(``respond_to_yield``), an external tool result, the cancel of a non-gate yield, the steer route's cancel of external calls and a ``wait_for_event`` delivery.

Machine wakes carry no gate, are delivered by event key alone and at least once, and one redelivered after the session re-parked under the same key would
decide the new park. The flip refuses a single park whose ``parked_at`` is another one. A graph park gets a fresh ``parked_at`` whenever a sibling is
resolved, so for a graph the stamp is not judged. Primer-internal (``__yield_`` prefix): stripped before any hook sees the payload (security ticket
01a1208d)."""


def with_wake_park(payload: "dict[str, Any]", parked_at: "datetime | None") -> "dict[str, Any]":
    """``payload`` plus the park its producer read (:data:`WAKE_PARK_KEY`); unchanged when the row has no ``parked_at``."""
    if parked_at is None:
        return payload
    return {**payload, WAKE_PARK_KEY: parked_at.isoformat()}


WAKE_ENTRY_KEY = "__yield_entry__"
"""Where a wake published by a producer that answers one specific pending ENTRY names it: the subscription of a trigger fire or a ``wait_for_event`` delivery
(``resume_metadata.subscription_id``), the call row of an external tool result (``resume_metadata.external_call_row_id``).

The park stamp (:data:`WAKE_PARK_KEY`) fences a single park only: a graph park re-parks with a fresh ``parked_at`` whenever a sibling is resolved, so it is
exempt. An entry's identity survives that, as a gate's id does, and the flip compares it with the entry that waits on the event key (ticket 01a1223f).
Primer-internal (``__yield_`` prefix): stripped before any hook sees the payload."""


def entry_id_of(resume_metadata: "dict[str, Any] | None") -> str | None:
    """The identity a machine producer can name for the pending entry whose ``resume_metadata`` this is, or ``None`` (a gate, a sleep, a park from before)."""
    meta = resume_metadata or {}
    return meta.get("subscription_id") or meta.get("external_call_row_id") or None


def with_wake_entry(payload: "dict[str, Any]", entry_id: str | None) -> "dict[str, Any]":
    """``payload`` plus the entry it answers (:data:`WAKE_ENTRY_KEY`); unchanged when the producer has no id to name."""
    if not entry_id:
        return payload
    return {**payload, WAKE_ENTRY_KEY: entry_id}


def with_wake_gate(payload: "dict[str, Any]", gate_id: str | None) -> "dict[str, Any]":
    """``payload`` plus the gate it decides (:data:`WAKE_GATE_ID_KEY`); unchanged for a gate with no id (a park from before gates had one)."""
    if not gate_id:
        return payload
    return {**payload, WAKE_GATE_ID_KEY: gate_id}


def wake_gate_id_of(raw_payload: "Any") -> str | None:
    """The gate a human decision's RAW wake payload names (:data:`WAKE_GATE_ID_KEY`), or ``None`` (a wake from before gates had ids, a machine wake, a reply that is not a dict).

    Read it from the raw wake: the resume classification strips the key from a real reply, so the payload a walk is handed no longer says which gate it decided."""
    named = raw_payload.get(WAKE_GATE_ID_KEY) if isinstance(raw_payload, dict) else None
    return named if isinstance(named, str) and named else None


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

        * ``timer:{session_id}:{tool_call_id}``: wakes from the timer scheduler (:func:`timer_event_key`; a park written before the key carried the
          session sits on ``timer:{tool_call_id}``).
        * ``ask_user:{session_id}:{tool_call_id}`` — wakes from
          the ``POST /v1/sessions/{id}/ask_user/respond`` endpoint.
          Inside a graph node it is ``ask_user:{session_id}:{node}:{tool_call_id}``
          (the fan-out instance id, as the approval key's), because two
          siblings can share a raw ``tool_call_id``.
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
    # Multi-event park: the full set of event keys the session waits on
    # (any one firing wakes it). None for the common single-event park,
    # which uses event_key alone.
    event_keys: list[str] | None = None

    def to_jsonable(self) -> dict[str, Any]:
        """Serialise for storage in the parked-state blob."""
        return {
            "tool_name": self.tool_name,
            "event_key": self.event_key,
            "timeout": self.timeout,
            "resume_metadata": dict(self.resume_metadata),
            "event_keys": self.event_keys,
        }

    @classmethod
    def from_jsonable(cls, data: dict[str, Any]) -> "Yielded":
        return cls(
            tool_name=data["tool_name"],
            event_key=data["event_key"],
            timeout=data.get("timeout"),
            resume_metadata=dict(data.get("resume_metadata") or {}),
            event_keys=data.get("event_keys"),
        )


#: The event-key prefixes of the parks that ask a PERSON to decide, with the turn-log ``yield_kind`` each is recorded
#: as. Every other key waits on something that needs no decision (a timer, a trigger, a remote task). One table for
#: the two readers that must agree on what a human gate is, the turn log's classifier in ``primer.session.dispatch``
#: and the agent loop's Stop handling, so a new gate kind cannot be added to one of them only.
YIELD_KIND_PREFIXES: tuple[tuple[str, str], ...] = (
    ("tool_approval:", "approval"),
    ("ask_user:", "ask_user"),
)


def asks_a_person(yielded: Yielded) -> bool:
    """True when ``yielded`` parks the session on a human decision (a tool approval or an answer to ask_user)."""
    key = yielded.event_key or ""
    return any(key.startswith(prefix) for prefix, _kind in YIELD_KIND_PREFIXES)


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
        Owning session's id. ``None`` for chat-only invocations,
        which carry ``chat_id`` instead.
    workspace_id
        Workspace id when the session is workspace-bound; ``None``
        for chat-only invocations.
    parked_at
        Set on resume to the timestamp the park was originally
        written; ``None`` on the initial call. Tools use this to
        compute how long they were parked for.
    chat_id
        Owning chat's id for chat-only invocations; ``None`` for
        session-bound invocations.
    inform
        Optional async sink for one-way inform delivery. Takes the
        message and returns the number of destinations reached.
        ``None`` when no channel/chat delivery is wired for this turn.
    graph_services
        Per-session GraphInvocationServices bundle for invoke_graph;
        ``None`` outside a workspace-session tool dispatch. Typed ``Any``
        to avoid a layering import cycle (primer.graph depends on this
        module, not vice versa).
    initiated_by
        Persisted attribution of the enclosing run, so a tool that
        CREATES a session (``create_workspace_session``) can stamp the
        child's ``initiated_by``. Set by :class:`ToolExecutionManager`
        from the enclosing workspace session's own ``initiated_by`` (or
        a richer per-call identity if the dispatch layer threads one);
        ``None`` outside a workspace-session tool dispatch, in which
        case the MCP session-create handler falls back to the system
        principal rather than fabricating a ``user`` attribution.
    turn_no
        The enclosing session turn's own turn number (01a0518b). Set by
        :class:`ToolExecutionManager` from the value its own caller
        threaded in at construction time. Unlike ``initiated_by`` this is
        NOT re-derived per nested call: ``system__invoke_agent`` reads it
        straight off this context and passes it verbatim into the
        subagent's own ``run_agent_turn`` call rather than minting a
        fresh one -- a nested subagent call belongs to the OUTER turn, so
        scoping it under a different turn_no would orphan its tool-call
        scoped ids from the record they need to pair against. ``None``
        outside a dispatch that resolved one (e.g. chat-only invocations,
        or before the caller opted into tool_calls_as_claims).
    """

    tool_call_id: str
    session_id: str | None
    workspace_id: str | None
    parked_at: datetime | None = None
    chat_id: str | None = None
    inform: Callable[[str], Awaitable[int]] | None = None
    graph_services: Any | None = None
    initiated_by: PrincipalRef | None = None
    turn_no: int | None = None


# ===========================================================================
# Control-flow exception (internal — escapes the LLM loop)
# ===========================================================================


#: The cancel reason the worker delivers to an execution whose lease it lost (``_CancelScope.cancel``): the
#: session may belong to another worker now, so the execution must not write to it on its way out.
CANCEL_REASON_PREEMPTED = "preempted"


def timer_event_key(ctx: "ToolContext", node_id: str | None = None) -> str:
    """The event key a timer park (``sleep``, a python tool's timer yield) waits on: ``timer:{session_id}:{tool_call_id}``.

    A provider repeats ``call_0``, ``call_1`` ... in every conversation, so a key made of the tool call id alone was shared by every session that slept under
    the same id, and one session's timer woke the others (ticket 01a12151-b225). The scope is the session id, else the chat id; a call with neither keeps the
    old key (there is nothing to wake). ``node_id`` is the graph node the call runs in (``current_graph_node_id()``, ``None`` outside a graph): two concurrent
    siblings of one superstep can share a raw provider id, so the node is folded in as ``ask_user``'s key does (``timer:{session_id}:{node}:{tool_call_id}``).
    A park written before this scoping keeps the key it has; the flip guards it by its deadline.
    """
    scope = ctx.session_id or ctx.chat_id
    if not scope:
        return f"timer:{ctx.tool_call_id}"
    if node_id is not None:
        return f"timer:{scope}:{node_id}:{ctx.tool_call_id}"
    return f"timer:{scope}:{ctx.tool_call_id}"


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
        # Unified nested-yield continuation stack. ALWAYS present (defaults
        # to an empty list) so callers can read/append ``yld.frames``
        # unconditionally. A nested invocation that re-raises this yield
        # (run_subagent / resume_subagent) prepends its own AgentFrame onto
        # this list; a session that yields directly leaves it empty.
        self.frames: list = []
        # Where in the OUTERMOST in-process tool batch the yield happened, and what had already finished there. Stamped
        # by ``primer.agent.loop._dispatch_tool_calls`` as the exception leaves it (the outermost loop stamps last, so
        # a nested yield carries the outer batch's position, not the subagent's), for a Stop that ends the park to
        # answer the round truthfully. ``tool_call_id`` cannot serve: for a nested yield it is the INNER call's raw
        # provider id, which restarts every stream and can equal an earlier outer id. A normal park ignores both.
        self.batch_index: int | None = None
        self.completed_results: list | None = None


class ToolWaitPark(Exception):
    """Raised by the claim-based tool-dispatch seam (Phase 3 stage 7a,
    01a0518b) when a batch of tool calls became independently-claimable
    ``ToolCallTask`` rows instead of executing sequentially in-process.

    Deliberately NOT a :class:`YieldToWorker` subclass and NOT built on
    :class:`Yielded` (leader ruling, 01a0518b): a subclass would
    silently degrade at any EXISTING generic ``except YieldToWorker``
    catch site (there are several, at multiple nesting layers — plain
    session dispatch, graph node dispatch, nested subagent invocation)
    — each would write classic single-call park state with a bogus
    ``tool_call_id`` for what is actually a batch park across N tasks.
    A genuinely separate exception type means an UNWIRED catch site
    does not catch this at all: it propagates as an unhandled
    exception and fails loudly, rather than parking the turn wrong.
    Every layer that should understand a tool_wait park must gain its
    own explicit ``except ToolWaitPark`` arm — same loud-over-silent
    principle as the aggregated-profile ``provider_id=None`` ruling.

    ``event_key`` is a synthetic, non-pub/sub identifier (mirrors how
    ``ToolCallTask.gate_event_key`` already works) — nothing ever
    publishes to it or subscribes on it; the actual wake trigger is
    the LAST outstanding task's ``on_release`` re-arming the session's
    claim lease directly (ruling 2), not an event-bus fire. It exists
    purely for parity with the session's own ``parked_event_key``
    column and for observability/debugging.

    Its tail is the batch's first outstanding id in whatever form the
    producer holds it, so it has no single id form. The agent loop
    (``_dispatch_as_claims``) and a graph's live-turn park hold scoped
    ids, and the live turn is the only place it is recorded (the turn
    log, the ``session.parked`` event and the YIELDED record). A graph
    RE-park (``_build_pending_tool_wait_park`` after ``restore_state``)
    can start from a carried-over checkpoint entry, which holds the
    session-qualified id (a bare one for a park written before S1b), or,
    when the cycle resolved every carried-over entry (``resume_from_
    checkpoint`` drops those) and a node dispatched a new batch, from a
    fresh scoped id.
    Neither producer has the session id ``external_call_id`` needs to
    normalise it, and nothing reads a re-park's value:
    ``_repark_graph_tool_wait_outcome`` falls back to it only when the
    checkpoint has no ``pending_tool_waits``, which a graph-raised park
    always has. Never key anything on it; ``tool_wait_event_key`` is the
    functional key.

    ``llm_messages`` / ``frames`` mirror :class:`YieldToWorker`'s own
    fields exactly, for the same reason: the in-progress turn's
    assistant message (carrying the tool_use parts the outstanding
    tasks correspond to) is not yet ``_persist_turn``'d when this
    fires, and the resume path needs it to reconstruct
    ``[assistant_tool_use, tool_result...]`` history correctly. Callers
    that raise this directly may leave ``llm_messages`` unset; the
    executor stamps it on the way out, same as ``YieldToWorker``.

    ``notifying_results`` (01a0518b, added when the dispatch seam
    landed): a notifying call in the SAME batch as a claimable one is
    answered inline as always (S3 spec section 3 — it never becomes a
    ``ToolCallTask``, it has nothing to park ON), but its result still
    needs a durable home the resume coordinator can find. Rather than a
    second, session-log-spelunking assembly path alongside the
    ``ToolCallTask`` rows, the ``except ToolWaitPark`` handler creates a
    terminal (``state=DONE``, ``result_state`` pre-populated) row for
    each notifying result too — so "read every sibling ``ToolCallTask``
    row for ``(session_id, turn_no)``" stays the SINGLE reassembly
    truth, notifying and claimable alike, rather than a split-brain
    between two sources. Each entry is ``(scoped_call_id,
    ToolResultPart)``: the scoped id (the row's own ``id`` is its
    session-qualified form, like every other ``ToolCallTask`` in the batch).

    ``call_ids`` (S1b): ``{scoped_call_id: provider_raw_id}`` for every
    call in the batch, claimable and notifying alike. The scoped ids
    above are unique within ONE session only and are what the
    transcript records and external surfaces carry; the row id the
    seam creates from each is session-qualified. The raw provider id
    is the only one on the LLM wire, so it rides to the row's
    ``call_id`` and every result is handed back to the model under it.
    """

    def __init__(
        self,
        *,
        outstanding_task_ids: list[str],
        event_key: str,
        llm_messages: list | None = None,
        notifying_results: "list[tuple[str, ToolResultPart]] | None" = None,
        call_ids: "dict[str, str] | None" = None,
    ) -> None:
        super().__init__(
            f"tool batch parked as {len(outstanding_task_ids)} "
            f"claimed task(s); event_key={event_key!r}"
        )
        self.outstanding_task_ids = outstanding_task_ids
        # scoped call id -> the provider's raw id, for every call in the batch (claimable and notifying): the
        # only id the LLM knows a call by, carried to the rows so results are handed back under it.
        self.call_ids: dict[str, str] = dict(call_ids or {})
        self.event_key = event_key
        self.llm_messages: list | None = llm_messages
        self.frames: list = []
        self.notifying_results: "list[tuple[str, ToolResultPart]]" = (
            notifying_results or []
        )


__all__ = [
    "GATE_ID_KEY",
    "GATE_ID_PATTERN",
    "new_gate_id",
    "gate_id_of",
    "WAKE_GATE_ID_KEY",
    "WAKE_ENTRY_KEY",
    "WAKE_PARK_KEY",
    "entry_id_of",
    "with_wake_entry",
    "with_wake_gate",
    "wake_gate_id_of",
    "with_wake_park",
    "timer_event_key",
    "Yielded",
    "YieldTimeout",
    "YieldCancelled",
    "ToolContext",
    "YieldToWorker",
    "ToolWaitPark",
    "YIELD_KIND_PREFIXES",
    "asks_a_person",
]
