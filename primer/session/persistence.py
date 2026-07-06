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
    StreamEvent,
    TextDelta,
    ToolCallEnd,
    ToolCallStart,
    Usage,
    _ExecutorToolResult,
    _GraphNodeEvent,
)
from primer.model.workspace_session import SessionMessageKind, SessionMessageRecord

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


def translate_stream_event(
    event: StreamEvent,
    state: _CoalesceState,
    node_id: str | None = None,
) -> "SessionMessageRecord | list[SessionMessageRecord] | None":
    """Per-event translation following the chat-selective persistence cadence.

    | Event                | Output                                          |
    |----------------------|-------------------------------------------------|
    | TextDelta            | None (coalesces into state.text_buffers[node])  |
    | Usage                | None (accumulated in state.last_usage_by[node]) |
    | ToolCallStart        | None (records name in state.tool_names[node,id])|
    | ToolCallEnd          | flush text buffer (if any), then TOOL_CALL      |
    | ExtendedEvent(_ExecutorToolResult) | TOOL_RESULT                    |
    | ExtendedEvent(_GraphNodeEvent) | reconstruct inner StreamEvent and    |
    |                      |   recurse with node_id=event.extended.node_id   |
    | Done                 | flush text buffer (if any), then DONE           |
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
        return translate_stream_event(inner, state, node_id=event.extended.node_id)

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
        return None

    if isinstance(event, ToolCallStart):
        # ToolCallStart carries the tool name; the paired ToolCallEnd (same
        # id) does not. Stash it so the TOOL_CALL record below can persist the
        # real name. Produces no record itself (the call is persisted on End).
        # Keyed by (node_id, id): synthesized ids can collide across
        # concurrent fan-out siblings, so node_id disambiguates.
        state.tool_names[(node_id, event.id)] = event.name
        return None

    if isinstance(event, ToolCallEnd):
        records: list[SessionMessageRecord] = []
        buffered = state.text_buffers.get(node_id, "")
        if buffered:
            records.append(
                SessionMessageRecord(
                    seq=1,
                    kind=SessionMessageKind.ASSISTANT_TOKEN,
                    payload={"text": buffered},
                    node_id=node_id,
                    created_at=now,
                )
            )
            state.text_buffers[node_id] = ""
        records.append(
            SessionMessageRecord(
                seq=1,
                kind=SessionMessageKind.TOOL_CALL,
                payload={
                    "id": event.id,
                    "name": state.tool_names.pop((node_id, event.id), None),
                    "arguments": event.arguments,
                },
                node_id=node_id,
                created_at=now,
            )
        )
        if len(records) == 1:
            return records[0]
        return records

    if isinstance(event, ExtendedEvent) and isinstance(
        event.extended, _ExecutorToolResult
    ):
        return SessionMessageRecord(
            seq=1,
            kind=SessionMessageKind.TOOL_RESULT,
            payload={
                "call_id": event.extended.call_id,
                "output": event.extended.output,
                "error": event.extended.error,
            },
            node_id=node_id,
            created_at=now,
        )

    if isinstance(event, Done):
        records = []
        buffered = state.text_buffers.get(node_id, "")
        if buffered:
            records.append(
                SessionMessageRecord(
                    seq=1,
                    kind=SessionMessageKind.ASSISTANT_TOKEN,
                    payload={"text": buffered},
                    node_id=node_id,
                    created_at=now,
                )
            )
            state.text_buffers[node_id] = ""
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
        state.last_usage_by.pop(node_id, None)
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

    # All other events (StreamStart, ReasoningDelta, ToolCallDelta, MediaDelta,
    # ExtendedEvent without _ExecutorToolResult / _GraphNodeEvent) — silently
    # dropped. (ToolCallStart is handled above: it records the tool name.)
    return None


__all__ = ["WorkspaceMessageWriter", "WorkspaceIO", "_CoalesceState", "translate_stream_event"]
