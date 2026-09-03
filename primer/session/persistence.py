"""Session message persistence — buffered jsonl appender.

``WorkspaceMessageWriter`` serialises :class:`SessionMessageRecord` objects
to newline-delimited JSON and appends them to
``<session-slot>/messages.jsonl`` in the workspace via an injected
``workspace_io`` dependency.

Buffer policy (amortises workspace I/O cost):
* Flush when accumulated bytes reach **16 KB**.
* Flush when the oldest buffered record is **100 ms** old.
* Flush on explicit :meth:`flush` or :meth:`aclose`.

Tick events fire **per-record** (not per-flush) so live WebSocket
subscribers see real-time deltas even when large batches are coalesced
into a single I/O write.

The writer owns the monotonic ``seq`` counter; the caller's
``record.seq`` is always overwritten with the writer's internal counter
so the stored value is authoritative.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from pydantic import TypeAdapter

from primer.model.chat import (
    Done,
    Error,
    ExtendedEvent,
    ReasoningDelta,
    StreamEvent,
    TextDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    Usage,
    _ClientAction,
    _ExecutorToolResult,
    _GraphNodeEvent,
    _LlmCall,
)
from primer.model.workspace_session import SessionMessageKind, SessionMessageRecord
from primer.tap.delta import (
    KIND_REASONING,
    KIND_TEXT,
    KIND_TOOL,
    part_id,
    scoped_tool_call_id,
)

# Reusable validator for the discriminated ``StreamEvent`` union.  Used to
# reconstruct the inner StreamEvent carried by a forwarded ``_GraphNodeEvent``
# from its json dump (``inner_payload`` already includes the ``type``
# discriminator — see primer.model.chat._GraphNodeEvent).  Built once at import
# time so per-event reconstruction is cheap.
_STREAM_EVENT_ADAPTER: TypeAdapter[StreamEvent] = TypeAdapter(StreamEvent)

# 16 KB flush threshold
_FLUSH_BYTES = 16 * 1024

# 100 ms flush age threshold (seconds)
_FLUSH_AGE_S = 0.100


class WorkspaceIO(Protocol):
    """Minimal interface the writer uses to persist message lines.

    The concrete implementations live on the workspace runtimes
    (added in Task 9).  Tests supply a :class:`FakeWorkspaceIO`.
    """

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        """Append a complete jsonl line (with trailing ``\\n``) to the session store."""
        ...

    async def append_state_line(
        self, workspace_id: str, relative_path: str, line: bytes,
    ) -> None:
        """Append ``line`` to ``relative_path`` inside the named workspace.

        Used by :class:`primer.observability.turn_log_writer.WorkspaceTurnLogWriter`
        to persist per-turn structured events at operator-controlled
        paths (typically ``.state/sessions/<sid>/turns.jsonl``).
        Implementations MUST be safe for concurrent callers writing
        to distinct paths.
        """
        ...


class WorkspaceMessageWriter:
    """Buffered jsonl appender for session messages.

    Buffers up to 100 ms or 16 KB to amortise workspace I/O cost.
    Tick events fire per-record (not per-flush) so live WS subscribers
    see real-time deltas.

    Args:
        workspace_io: Dependency satisfying :class:`WorkspaceIO`.
        session_id: Identifies the workspace session being written.
        start_seq: Initial value of the internal seq counter (default 0).
            The first appended record gets ``start_seq + 1``. Callers
            that append to a session with existing history (e.g.
            ``reset_session`` writing an invocation divider) pass the
            row's current ``last_seq`` so seqs stay monotonic.
    """

    def __init__(
        self, *, workspace_io: WorkspaceIO, session_id: str, start_seq: int = 0,
    ) -> None:
        self._io = workspace_io
        self._session_id = session_id
        self._seq: int = start_seq

        # Buffer state
        self._buffer: list[bytes] = []
        self._buffer_size: int = 0
        self._oldest_at: float | None = None  # monotonic clock at first buffered record

    @property
    def last_seq(self) -> int:
        """The highest seq assigned so far (== ``start_seq`` before any append).

        Callers persist this back to the session row's ``last_seq`` at turn
        boundaries so the next turn's writer (and any concurrent
        ``wake_session``/``reset_session``) seed past this turn's records and
        ``(session_id, seq)`` stays monotonic across turns.
        """
        return self._seq

    async def append(self, record: SessionMessageRecord) -> int:
        """Append a record; flush per buffer policy.

        The writer overwrites ``record.seq`` with its own monotonic counter.

        Returns:
            The assigned seq number (1-based, monotonically increasing).
        """
        # Assign writer-controlled seq
        self._seq += 1
        assigned_seq = self._seq

        # Rebuild with the correct seq
        record = record.model_copy(update={"seq": assigned_seq})

        # Serialise to jsonl line
        line: bytes = record.model_dump_json().encode() + b"\n"

        # --- tick event fires per-record (before buffering) ---
        # The ``session:{sid}:tick`` bus event is published by the dispatch
        # layer (``primer/session/dispatch.py``) after each append; the
        # WorkspaceTapRouter consumes those ticks to drive the tap.

        # Check if we should flush before buffering (age policy)
        if self._oldest_at is not None:
            age = time.monotonic() - self._oldest_at
            if age >= _FLUSH_AGE_S:
                await self._do_flush()

        # Add to buffer
        self._buffer.append(line)
        self._buffer_size += len(line)
        if self._oldest_at is None:
            self._oldest_at = time.monotonic()

        # Check size policy after buffering
        if self._buffer_size >= _FLUSH_BYTES:
            await self._do_flush()

        return assigned_seq

    async def flush(self) -> None:
        """Flush all buffered records to workspace storage."""
        await self._do_flush()

    async def aclose(self) -> None:
        """Flush remaining records and release resources."""
        await self._do_flush()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _do_flush(self) -> None:
        """Write the current buffer to workspace_io and reset it."""
        if not self._buffer:
            return
        combined = b"".join(self._buffer)
        self._buffer = []
        self._buffer_size = 0
        self._oldest_at = None
        await self._io.append_message_line(self._session_id, combined)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _CoalesceState:
    """Holds the in-progress TextDelta buffer so consecutive deltas
    coalesce into a single assistant_token record on Done/ToolCallEnd.

    Also accumulates the most-recent Usage event so that the DONE record
    can carry a ``usage`` envelope — the LLM adapters emit Usage mid-stream
    (Anthropic/Google: cumulative on every chunk; OpenAI/Ollama: terminal
    only) and Done itself carries no token counts.

    **Per-node keying.** Both the text buffer and the accumulated Usage are
    keyed by ``node_id`` (``None`` = the plain agent-only path). Concurrent
    graph fan-out nodes interleave their events in a single merged stream, so
    a single shared buffer would mix sibling nodes' text and let one node's
    Done carry a sibling's usage. Keying by node_id isolates each node's
    coalescing. ``None`` keeps the agent-only path byte-identical to before
    (one bucket, the same flush points).

    Cosmetic note (F4): a node's ``graph_transition`` record is emitted
    immediately (it never buffers), so it can interleave seq-wise with a
    *concurrent* sibling node's still-buffered text. This is accepted as
    cosmetic — seqs stay monotonic and nothing is lost; flush ordering is
    unchanged.
    """

    text_buffers: dict[str | None, str] = field(default_factory=dict)
    # Model reasoning / extended thinking, coalesced exactly like the
    # answer text. Flushed as a REASONING record BEFORE the buffered
    # answer at every flush point, so the transcript orders thought ->
    # action the way the model produced them. Until 2026-08-25 the
    # adapters' ReasoningDelta events were silently dropped here and
    # thinking never reached the transcript at all.
    reasoning_buffers: dict[str | None, str] = field(default_factory=dict)
    last_usage_by: dict[str | None, Usage] = field(default_factory=dict)
    # Tool name carried from ToolCallStart (which has it) to the paired
    # ToolCallEnd (same id, but no name field), keyed by (node_id, tool_call
    # id) — so the TOOL_CALL record can persist the real name instead of the
    # UI's generic "tool" fallback. Popped on ToolCallEnd. The LLM adapters
    # synthesize call ids from a per-stream counter (e.g. "call_0"), so bare
    # ids restart at the same value on every stream — concurrent graph
    # fan-out siblings can legitimately share an id. Keying by node_id too
    # (mirroring text_buffers/last_usage_by above) keeps siblings isolated.
    # An unmatched ToolCallStart (turn cancelled before its ToolCallEnd)
    # leaves a dangling entry, but it's bounded to this _CoalesceState's
    # lifetime (one run) — not worth cleanup machinery.
    tool_names: dict[tuple[str | None, str], str] = field(default_factory=dict)
    # 01a0518f: per-node monotonic counter, incremented once per
    # ToolCallStart, feeding the scoped tool-call id below. node_id alone
    # disambiguates SIBLINGS sharing a raw id within the same tool-round,
    # but not two DIFFERENT tool-rounds on the SAME node (loop.py issues a
    # fresh llm.stream() per round, and each adapter's id counter resets
    # every stream call — see persistence.py's module comment / the
    # tool_names note above) — the seq closes that gap too.
    tool_call_seq: dict[str | None, int] = field(default_factory=dict)
    # Raw provider id -> scoped id ("<node>:tool:<turn_no>:<seq>", mirrors
    # part_id()'s node:kind:turn_no convention with a seq appended since,
    # unlike text/reasoning, more than one tool call can happen per node
    # per turn). Minted at ToolCallStart (before ToolCallDelta needs it),
    # read (not popped) through ToolCallEnd, and popped at the matching
    # _ExecutorToolResult — the one guaranteed-last consumer, since
    # loop.py's tool-rounds are strictly sequential (every round's
    # _ExecutorToolResult events land before the NEXT round's
    # ToolCallStart can reuse the same raw id). Keyed by (node_id, raw_id)
    # like tool_names above, for the identical reason.
    scoped_call_ids: dict[tuple[str | None, str], str] = field(default_factory=dict)
    # (turn_no, coalesced text) of the most recent ASSISTANT_TOKEN record
    # actually written to the log, whichever node produced it — a plain
    # global tracker, not per-node, because "the immediately preceding
    # ASSISTANT_TOKEN" is a messages.jsonl-order concept, not a graph-
    # topology one. Live finding 01a064d3: a graph's End node renders its
    # own ASSISTANT_TOKEN from output_template (see the _GraphEndOutputEvent
    # branch below); when that template is a passthrough of the immediately
    # preceding node's answer, the two records are byte-identical and the
    # transcript shows the same paragraph twice. Set wherever a real
    # ASSISTANT_TOKEN record is emitted, consulted only by that branch.
    last_assistant_token: tuple[int, str] | None = None


class _DeltaSink(Protocol):
    """Duck-typed ephemeral-delta sink (``primer.tap.delta.DeltaBuffer``).

    ``translate_stream_event`` is synchronous, so it can only drive the
    *synchronous* half of the buffer: :meth:`on_delta` per content delta and
    :meth:`close` when the durable record for a part is produced. The buffer
    publishes its own frames on a separate async cadence; the translator
    never blocks on I/O. Absent (``None``) the live path is skipped and the
    durable-record behaviour is byte-identical to before.
    """

    def on_delta(self, pid: str, kind: str, delta: str) -> None: ...

    def close(self, pid: str) -> None: ...


def translate_stream_event(
    event: StreamEvent,
    state: _CoalesceState,
    node_id: str | None = None,
    delta_sink: "_DeltaSink | None" = None,
    turn_no: int = 0,
) -> "SessionMessageRecord | list[SessionMessageRecord] | None":
    """Per-event translation following the chat-selective persistence cadence.

    | Event                | Output                                          |
    |----------------------|-------------------------------------------------|
    | TextDelta            | None (coalesces into state.text_buffers[node])  |
    | ReasoningDelta       | None (coalesces into state.reasoning_buffers)   |
    | Usage                | None (accumulated in state.last_usage_by[node]) |
    | ToolCallStart        | None (records name in state.tool_names[node,id])|
    | ToolCallEnd          | flush reasoning, then text, then TOOL_CALL      |
    | ExtendedEvent(_ExecutorToolResult) | TOOL_RESULT                    |
    | ExtendedEvent(_ClientAction)       | CLIENT_ACTION                  |
    | ExtendedEvent(_LlmCall)            | LLM_CALL                       |
    | ExtendedEvent(_GraphNodeEvent) | reconstruct inner StreamEvent and    |
    |                      |   recurse with node_id=event.extended.node_id   |
    | Done                 | flush reasoning + text buffers, then DONE       |
    |                      |   payload includes usage envelope when present  |
    | Error                | ERROR                                           |
    | _GraphErrorEvent     | ERROR (graph runtime terminal failure)          |
    | _GraphTransitionEvent | GRAPH_TRANSITION (node enter/exit boundary)    |
    | _GraphEndOutputEvent | ASSISTANT_TOKEN (graph End-node output)         |
    | (others)             | None — silently dropped                         |

    ``node_id`` attributes every produced record to its originating graph
    node. The default ``None`` is the plain agent-only path and is preserved
    byte-for-byte (records carry ``node_id=None``, coalescing uses the
    ``None`` bucket). Forwarded per-node agent events arrive wrapped in an
    ``ExtendedEvent(_GraphNodeEvent)``; that branch reconstructs the inner
    StreamEvent and recurses, supplying the wrapper's ``node_id`` — so the
    caller (session dispatch) never passes ``node_id`` itself.

    Worker code is responsible for synthetic kinds (USER_INPUT, CANCELLED,
    YIELDED, RESUMED) — not produced by this translator from LLM events.

    ``turn_no`` (01a04e02) rides into every ``part_id()`` call below - the
    caller's current turn (``session.turn_no``, stable for the whole of
    ``run_one_session_turn``) so a text/reasoning part's id never repeats
    across turns on the same node. Defaults to 0 for callers that don't
    care (most unit tests); production call sites always pass the real
    value.
    """
    now = _now_utc()

    # Graph runtime terminal-failure event (spec §5.4) and End-node
    # output event (spec §4.4 / §2.2). Imported locally to avoid a
    # hard import-time dependency from primer.session on primer.graph
    # (the latter brings in jinja2 + jsonschema, which the agent-only
    # session path doesn't need).
    from primer.graph.base import (
        _GraphEndOutputEvent,
        _GraphErrorEvent,
        _GraphTransitionEvent,
    )

    # Per-node agent event forwarded by the graph executor (it wraps every
    # child agent event in ``ExtendedEvent(_GraphNodeEvent(...))``, carrying
    # node_id). Un-drop it: reconstruct the inner StreamEvent from its json
    # dump and recurse with the wrapper's node_id so the inner event is
    # persisted exactly as it would be on the agent-only path, but attributed
    # to the node. ``inner_payload`` is a ``model_dump(mode="json")`` that
    # already includes the ``type`` discriminator, so the union adapter can
    # re-validate it directly. NOTE the nesting case: a node's tool result
    # arrives as _GraphNodeEvent wrapping an ExtendedEvent(_ExecutorToolResult)
    # — reconstruction yields that ExtendedEvent and the recursion lands on the
    # TOOL_RESULT branch below.
    if isinstance(event, ExtendedEvent) and isinstance(event.extended, _GraphNodeEvent):
        try:
            inner = _STREAM_EVENT_ADAPTER.validate_python(event.extended.inner_payload)
        except Exception:
            # Inner event isn't a reconstructable StreamEvent — drop, exactly
            # as an unhandled event would be dropped on the agent path.
            return None
        return translate_stream_event(
            inner, state, node_id=event.extended.node_id, delta_sink=delta_sink,
            turn_no=turn_no,
        )

    if isinstance(event, _GraphTransitionEvent):
        # Graph-runtime node-lifecycle transition (spec §2.6). Maps 1:1 to a
        # graph_transition record whose payload stays small; record_to_tap_event
        # turns it into a TapEventClass.GRAPH_TRANSITION event for the tap.
        #
        # F4 (cosmetic interleave): this record is emitted immediately and never
        # buffers, so its seq may land between a *concurrent* sibling node's
        # buffered TextDeltas and that sibling's flush. Accepted as cosmetic —
        # seqs stay monotonic and nothing is lost; flush ordering is unchanged.
        return SessionMessageRecord(
            seq=1,  # WorkspaceMessageWriter overwrites
            kind=SessionMessageKind.GRAPH_TRANSITION,
            payload={
                "node_id": event.node_id,
                "node_kind": event.node_kind,
                "phase": event.phase,
                "status": event.status,
            },
            node_id=event.node_id,
            created_at=now,
        )

    if isinstance(event, _GraphErrorEvent):
        return SessionMessageRecord(
            seq=1,  # WorkspaceMessageWriter overwrites
            kind=SessionMessageKind.ERROR,
            payload={
                "code": event.code,
                "message": event.message,
                "node_id": event.node_id,
                "path": event.path,
            },
            node_id=event.node_id,
            created_at=now,
        )

    if isinstance(event, _GraphEndOutputEvent):
        # Live finding 01a064d3: two suppressions, both approved rulings,
        # a distinct record kind for graph results (the long-term shape)
        # deliberately deferred to Phase 3 stage 7a's record-vocabulary
        # work rather than done here as part of a bug fix.
        #
        # (c) An End node with no/empty output_template renders "" -
        # writing that as an ASSISTANT_TOKEN is pure noise in every graph
        # transcript, so skip it outright rather than persist an empty
        # answer bubble.
        if not event.text:
            return None
        # (a) A passthrough output_template (the common case: End just
        # echoes the last node's answer) renders byte-identical text to
        # the ASSISTANT_TOKEN immediately preceding it in THIS turn - the
        # worker's answer IS the graph's result, so a second record adds
        # no information, only a visible duplicate paragraph. Compare the
        # final COALESCED text (state.last_assistant_token), never raw
        # deltas, and only within the same turn_no - a genuine
        # transformation (the template actually changes the text) still
        # gets its own finish-attributed record, which is semantically
        # correct. Fragility bound, accepted: a template that changes
        # only whitespace still writes both records (byte equality, not
        # a semantic diff).
        if state.last_assistant_token == (turn_no, event.text):
            return None
        state.last_assistant_token = (turn_no, event.text)
        return SessionMessageRecord(
            seq=1,  # WorkspaceMessageWriter overwrites
            kind=SessionMessageKind.ASSISTANT_TOKEN,
            payload={
                "text": event.text,
                "parsed": event.parsed,
                "end_node_id": event.end_node_id,
            },
            node_id=event.end_node_id,
            created_at=now,
        )

    if isinstance(event, Usage):
        # Accumulate so the DONE record can carry a usage envelope.  Providers
        # that emit cumulative counts (Anthropic, Google) overwrite on every
        # chunk; terminal-only providers (OpenAI, Ollama) set it once.  Keyed
        # by node_id so concurrent fan-out siblings don't clobber each other's
        # token counts (None = agent-only path).
        state.last_usage_by[node_id] = event
        return None

    if isinstance(event, TextDelta):
        # Keyed by node_id so interleaved sibling-node text never mixes.
        state.text_buffers[node_id] = state.text_buffers.get(node_id, "") + event.text
        if delta_sink is not None:
            delta_sink.on_delta(part_id(node_id, KIND_TEXT, turn_no), KIND_TEXT, event.text)
        return None

    if isinstance(event, ReasoningDelta):
        # Same coalescing discipline as TextDelta; flushed as a
        # REASONING record at the same flush points.
        state.reasoning_buffers[node_id] = (
            state.reasoning_buffers.get(node_id, "") + event.text
        )
        if delta_sink is not None:
            delta_sink.on_delta(
                part_id(node_id, KIND_REASONING, turn_no), KIND_REASONING, event.text
            )
        return None

    if isinstance(event, ToolCallStart):
        # ToolCallStart carries the tool name; the paired ToolCallEnd (same
        # id) does not. Stash it so the TOOL_CALL record below can persist the
        # real name. Produces no record itself (the call is persisted on End).
        # Keyed by (node_id, id): synthesized ids can collide across
        # concurrent fan-out siblings, so node_id disambiguates.
        state.tool_names[(node_id, event.id)] = event.name
        # 01a0518f: mint the scoped id NOW (not at End) - ToolCallDelta,
        # which arrives between Start and End, needs the same id the
        # durable TOOL_CALL record will carry, for live-arguments
        # reconciliation to work. See _CoalesceState.scoped_call_ids.
        seq = state.tool_call_seq.get(node_id, 0) + 1
        state.tool_call_seq[node_id] = seq
        state.scoped_call_ids[(node_id, event.id)] = scoped_tool_call_id(
            node_id, turn_no, seq
        )
        return None

    if isinstance(event, ToolCallDelta):
        # Like TextDelta this coalesces into the single TOOL_CALL record
        # (produced on ToolCallEnd), so it produces no durable record of its
        # own - but it feeds the delta sink so a client can render the
        # arguments as they stream. The part_id is the SCOPED tool-call id
        # (01a0518f - was the raw id verbatim), because the paired TOOL_CALL
        # record carries the same scoped id and the client reconciles the
        # live arguments to it by that id. Falls back to the raw id if
        # ToolCallStart's mint is missing (defensive; every real adapter
        # emits Start before Delta).
        if delta_sink is not None:
            scoped_id = state.scoped_call_ids.get((node_id, event.id), event.id)
            delta_sink.on_delta(scoped_id, KIND_TOOL, event.arguments_delta)
        return None

    if isinstance(event, ToolCallEnd):
        records: list[SessionMessageRecord] = []
        thought = state.reasoning_buffers.get(node_id, "")
        if thought:
            records.append(
                SessionMessageRecord(
                    seq=1,
                    kind=SessionMessageKind.REASONING,
                    payload={
                        "text": thought,
                        "part_id": part_id(node_id, KIND_REASONING, turn_no),
                    },
                    node_id=node_id,
                    created_at=now,
                )
            )
            state.reasoning_buffers[node_id] = ""
        buffered = state.text_buffers.get(node_id, "")
        if buffered:
            records.append(
                SessionMessageRecord(
                    seq=1,
                    kind=SessionMessageKind.ASSISTANT_TOKEN,
                    payload={
                        "text": buffered,
                        "part_id": part_id(node_id, KIND_TEXT, turn_no),
                    },
                    node_id=node_id,
                    created_at=now,
                )
            )
            state.text_buffers[node_id] = ""
            state.last_assistant_token = (turn_no, buffered)
        # 01a0518f: the durable TOOL_CALL id is the SCOPED id minted at
        # ToolCallStart (was the raw provider id verbatim, which
        # restarts at the same value every llm.stream() call and can
        # collide across tool-rounds and concurrent fan-out siblings -
        # see _CoalesceState.scoped_call_ids). Read, not popped: the
        # LATER _ExecutorToolResult event for this same call still needs
        # to resolve back to it.
        scoped_id = state.scoped_call_ids.get((node_id, event.id), event.id)
        records.append(
            SessionMessageRecord(
                seq=1,
                kind=SessionMessageKind.TOOL_CALL,
                payload={
                    "id": scoped_id,
                    "name": state.tool_names.pop((node_id, event.id), None),
                    "arguments": event.arguments,
                    # 01a0518f: the raw provider id, preserved alongside
                    # the scoped "id" above. primer.session.delegation's
                    # DelegationRecorder stamps a delegated record's
                    # payload["delegate_tool_call_id"] with the raw id
                    # (it never sees this _CoalesceState's scoped-id
                    # minting - a genuinely separate _CoalesceState
                    # instance for the subagent's own content), so
                    # primer.session.timeline's delegation-nesting lookup
                    # needs the raw id to find its parent, not the scoped
                    # one. Every OTHER consumer keys off "id"/"call_id"
                    # (the scoped id) as usual - this field exists solely
                    # for that one cross-_CoalesceState-boundary lookup.
                    "raw_id": event.id,
                },
                node_id=node_id,
                created_at=now,
            )
        )
        # The text/reasoning parts end here; the tool-input part ends here too
        # (its part_id is the SCOPED tool call id, matching what ToolCallDelta
        # opened it under above). A part with no deltas is a no-op.
        if delta_sink is not None:
            delta_sink.close(part_id(node_id, KIND_TEXT, turn_no))
            delta_sink.close(part_id(node_id, KIND_REASONING, turn_no))
            delta_sink.close(scoped_id)
        if len(records) == 1:
            return records[0]
        return records

    if isinstance(event, ExtendedEvent) and isinstance(event.extended, _LlmCall):
        call = event.extended
        return SessionMessageRecord(
            seq=1,  # WorkspaceMessageWriter overwrites
            kind=SessionMessageKind.LLM_CALL,
            payload={
                "profile_id": call.profile_id,
                "provider_id": call.provider_id,
                "model": call.model,
                "input_tokens": call.input_tokens,
                "output_tokens": call.output_tokens,
                "duration_ms": call.duration_ms,
                "status": call.status,
            },
            node_id=node_id,
            created_at=now,
        )

    if isinstance(event, ExtendedEvent) and isinstance(
        event.extended, _ExecutorToolResult
    ):
        # 01a0518f: resolve back to the SAME scoped id the paired
        # TOOL_CALL record carries (was the raw provider id verbatim) -
        # this is the LAST consumer of the mapping entry, so pop it
        # (loop.py's tool-rounds are strictly sequential: every round's
        # results land before the next round's ToolCallStart could reuse
        # the same raw id, so popping here can't race a later Start for
        # the same key). Falls back to the raw id if the mapping is
        # somehow missing (defensive; every real dispatch pairs a
        # ToolCallEnd with exactly one later result).
        scoped_call_id = state.scoped_call_ids.pop(
            (node_id, event.extended.call_id), event.extended.call_id,
        )
        return SessionMessageRecord(
            seq=1,
            kind=SessionMessageKind.TOOL_RESULT,
            payload={
                "call_id": scoped_call_id,
                "output": event.extended.output,
                "error": event.extended.error,
                # UX reconcile wave 5: a workspace tool's own extra data
                # (grep's match_count/file_count, ...) used to be dropped
                # here, the last of three drop points on this path
                # (ToolResultPart -> _ExecutorToolResult -> here). Additive
                # and defensive: a record persisted before this field
                # existed simply has no "metadata" key, and every reader
                # of this payload already treats it as optional.
                "metadata": event.extended.metadata,
            },
            node_id=node_id,
            created_at=now,
        )

    if isinstance(event, ExtendedEvent) and isinstance(
        event.extended, _ClientAction
    ):
        # 01a0518f: resolve to the SAME scoped id the paired TOOL_CALL
        # record carries (was the raw id verbatim) - a client-delivered
        # tool's streaming ToolCallEnd has already run by the time
        # _dispatch_tool_calls builds this event (loop.py dispatches
        # AFTER the assistant message is fully built), so the mapping is
        # already populated. Read, not popped: the notifying contract
        # still emits a TOOL_RESULT after this delivery frame (loop.py's
        # own comment: "tool_call -> client_action -> tool_result"),
        # which is the actual last consumer.
        return SessionMessageRecord(
            seq=1,
            kind=SessionMessageKind.CLIENT_ACTION,
            payload={
                "call_id": state.scoped_call_ids.get(
                    (node_id, event.extended.call_id), event.extended.call_id,
                ),
                "name": event.extended.name,
                "arguments": dict(event.extended.arguments or {}),
            },
            node_id=node_id,
            created_at=now,
        )

    if isinstance(event, Done):
        records = []
        thought = state.reasoning_buffers.get(node_id, "")
        if thought:
            records.append(
                SessionMessageRecord(
                    seq=1,
                    kind=SessionMessageKind.REASONING,
                    payload={
                        "text": thought,
                        "part_id": part_id(node_id, KIND_REASONING, turn_no),
                    },
                    node_id=node_id,
                    created_at=now,
                )
            )
            state.reasoning_buffers[node_id] = ""
        buffered = state.text_buffers.get(node_id, "")
        if buffered:
            records.append(
                SessionMessageRecord(
                    seq=1,
                    kind=SessionMessageKind.ASSISTANT_TOKEN,
                    payload={
                        "text": buffered,
                        "part_id": part_id(node_id, KIND_TEXT, turn_no),
                    },
                    node_id=node_id,
                    created_at=now,
                )
            )
            state.text_buffers[node_id] = ""
            state.last_assistant_token = (turn_no, buffered)
        done_payload: dict = {"stop_reason": event.stop_reason, "raw_reason": event.raw_reason}
        last_usage = state.last_usage_by.get(node_id)
        if last_usage is not None:
            u = last_usage
            usage_dict: dict = {
                "input_tokens": u.input_tokens,
                "output_tokens": u.output_tokens,
            }
            if u.cached_input_tokens is not None:
                usage_dict["cached_input_tokens"] = u.cached_input_tokens
            if u.reasoning_tokens is not None:
                usage_dict["reasoning_tokens"] = u.reasoning_tokens
            done_payload["usage"] = usage_dict
        done_record = SessionMessageRecord(
            seq=1,
            kind=SessionMessageKind.DONE,
            payload=done_payload,
            node_id=node_id,
            created_at=now,
        )
        # Done is terminal for this (node) stream within the coalesce state:
        # drop its per-node buffers so a stray second Done can't replay a
        # stale usage envelope (mirrors the text-buffer clear discipline) and
        # the dicts don't accumulate dead keys across many nodes.
        state.text_buffers.pop(node_id, None)
        state.reasoning_buffers.pop(node_id, None)
        state.last_usage_by.pop(node_id, None)
        if delta_sink is not None:
            delta_sink.close(part_id(node_id, KIND_TEXT, turn_no))
            delta_sink.close(part_id(node_id, KIND_REASONING, turn_no))
        if records:
            records.append(done_record)
            return records
        return done_record

    if isinstance(event, Error):
        return SessionMessageRecord(
            seq=1,
            kind=SessionMessageKind.ERROR,
            payload={"message": event.message, "code": event.code, "fatal": event.fatal},
            node_id=node_id,
            created_at=now,
        )

    # All other events (StreamStart, ToolCallDelta, MediaDelta,
    # ExtendedEvent without _ExecutorToolResult / _GraphNodeEvent) — silently
    # dropped. (ToolCallStart is handled above: it records the tool name.)
    return None


def stash_graph_scoped_ids(
    graph_checkpoint: dict[str, Any] | None, coalesce_state: "_CoalesceState",
) -> dict[str, int]:
    """Stash node-qualified scoped tool-call ids into a graph checkpoint's
    pending entries, at the moment a ``_CoalesceState`` that minted them is
    still in scope (01a0690a — the graph-path sibling of 0b4e8bfc's
    ``ParkedState.scoped_tool_call_id`` for the agent path).

    Two call sites share this: dispatch.py's top-level ``except
    YieldToWorker`` catch (a fresh park, ``coalesce_state`` populated by the
    live turn's own ``translate_stream_event`` calls via the
    ``_GraphNodeEvent`` unwrap), and the graph-resume drain's own repark
    catch (worker/graph_resume.py — a FRESH ``_CoalesceState`` seeded from
    the checkpoint's prior mints, see ``ParkedState.node_tool_call_seq``).

    Mutates ``pending_toolcalls``/``pending_agent_yields`` entries IN PLACE,
    setting ``scoped_tool_call_id`` from ``coalesce_state.scoped_call_ids``
    keyed by ``(entry["node_id"], entry["tool_call_id"])`` — but only when
    the entry doesn't already carry one: an entry stashed by an EARLIER park
    in this same checkpoint's history (carried forward through a repark)
    keeps its original id rather than being re-derived (there is nothing to
    re-derive from a resume-time ``coalesce_state`` that never minted it).

    Returns a snapshot of ``coalesce_state.tool_call_seq`` (the per-node
    monotonic mint counters) for ``ParkedState.node_tool_call_seq`` — {} for
    an agent-bound park (``graph_checkpoint is None``, nothing to stash).
    """
    if graph_checkpoint is None:
        return {}
    for entries_key in ("pending_toolcalls", "pending_agent_yields"):
        for entry in graph_checkpoint.get(entries_key) or []:
            if entry.get("scoped_tool_call_id") is not None:
                continue
            sid = coalesce_state.scoped_call_ids.get(
                (entry.get("node_id"), entry.get("tool_call_id"))
            )
            if sid is not None:
                entry["scoped_tool_call_id"] = sid
    return dict(coalesce_state.tool_call_seq)


def infer_agent_phase(event: StreamEvent) -> str | None:
    """Map a raw StreamEvent to the agent_phase (01a04d91-a7a0) it
    implies, or ``None`` if this event kind carries no phase information
    (Usage, ToolCallDelta, an unmatched ExtendedEvent, etc.).

    Deliberately operates on the RAW event, before translate_stream_event's
    coalescing: TextDelta/ReasoningDelta only produce a durable record at a
    flush point (ToolCallEnd or Done), which would tell a live phase signal
    about "responding" started far too late — the whole point of the phase
    field is to be true WHILE the tokens are streaming, not after they've
    already been buffered into a record. A pure function, no side effects,
    so the caller (dispatch.run_one_session_turn) decides what to do with a
    transition (only writing/publishing on an actual change, not every
    event) rather than this function doing it inline.

    * ReasoningDelta -> "thinking" (reasoning renders as its own
      collapsible block, distinct from the final answer - see agent_phase's
      own docstring on WorkspaceSession).
    * TextDelta -> "responding" (the final answer has started).
    * ToolCallStart -> "executing".
    * ToolCallEnd -> "thinking" (the tool result is about to be appended
      and the agent will re-request a completion).
    * Done / Error -> "waiting" (the turn is ending; dispatch's own
      turn-status cleanup already fires around the same moment).
    * Everything else -> None (no transition implied).
    """
    if isinstance(event, ReasoningDelta):
        return "thinking"
    if isinstance(event, TextDelta):
        return "responding"
    if isinstance(event, ToolCallStart):
        return "executing"
    if isinstance(event, ToolCallEnd):
        return "thinking"
    if isinstance(event, (Done, Error)):
        return "waiting"
    return None


__all__ = [
    "WorkspaceMessageWriter", "WorkspaceIO", "_CoalesceState",
    "translate_stream_event", "stash_graph_scoped_ids",
]
