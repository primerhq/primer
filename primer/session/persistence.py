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

A batch the workspace never answers (its runtime connection dropped) does not hold the writer, or whoever flushes it, for ever: a flush
waits for a batch in flight at most ``_WRITE_TIMEOUT_S`` after it started, then abandons it and BREAKS the writer (see
:meth:`WorkspaceMessageWriter._do_flush`).

The writer owns the monotonic ``seq`` counter; the caller's
``record.seq`` is always overwritten with the writer's internal counter
so the stored value is authoritative.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

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
    _CompactionNote,
    _ExecutorToolResult,
    _GraphNodeEvent,
    _LlmCall,
)
from primer.model.except_ import WorkspaceUnreachableError
from primer.model.workspace_session import SessionMessageKind, SessionMessageRecord
from primer.observability import metrics as _metrics
from primer.session import write_clock
from primer.tap.delta import (
    KIND_REASONING,
    KIND_TEXT,
    KIND_TOOL,
    part_id,
    scoped_tool_call_id,
)

logger = logging.getLogger(__name__)

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

# How long a request may stay unanswered, from the moment it is SENT (the backend has taken the session's messages_lock; see
# ``primer.session.write_clock``), before a flush stops waiting for its batch and abandons it. A write that takes this long means the
# workspace is not accepting writes (its runtime connection dropped, which a client that waits for the answer never reports); a
# reconnect after a pod reschedule counts, so a deployment that reschedules slowly raises it. Generous on purpose: an abandoned batch is
# lost for good and the writer closed. Set from ``AppConfig.session_message_write_timeout_seconds`` when the app is created
# (:func:`configure_write_timeout`); a module value, read when a flush waits, so a test can shorten it.
_WRITE_TIMEOUT_S = 30.0

# How long a batch may wait in all, counting the wait for the messages_lock that the bound above leaves out, in bounds. A lock holder
# that is itself stuck on the dead connection never lets go, and waiting for it without limit would be the original hang again.
_QUEUE_CAP_FACTOR = 4


def configure_write_timeout(seconds: float) -> None:
    """Set the write bound (``AppConfig.session_message_write_timeout_seconds``). Must be a finite number above zero."""
    global _WRITE_TIMEOUT_S  # noqa: PLW0603
    if not (seconds > 0 and math.isfinite(seconds)):
        raise ValueError(f"the message write timeout must be a finite number of seconds above zero, got {seconds!r}")
    _WRITE_TIMEOUT_S = float(seconds)

# Strong references to the appends in flight (see ``WorkspaceMessageWriter._do_flush``): the loop keeps only weak ones to its
# tasks, and an append whose flushing task was cancelled has nobody else holding it.
_APPENDS: set[asyncio.Task[None]] = set()


def _append_done(task: asyncio.Task[None]) -> None:
    _APPENDS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()          # retrieved; the flushing task, if it is still waiting, gets the same exception
    if exc is not None:
        logger.warning("a batch of session message records could not be appended: %r", exc)


class WorkspaceWriteTimeout(TimeoutError, WorkspaceUnreachableError):
    """The workspace did not accept a batch of message records within ``_WRITE_TIMEOUT_S``, so the writer is closed.

    Raised by the flush that gave up, and then by every append and flush of the broken writer, at once. A ``TimeoutError``, so the exits
    that already treat a write that never returns as "carry on without it" (the best-effort and the CANCELLED-record writes) take it as
    they take their own bound; and a ``WorkspaceUnreachableError``, so any route that lets it out answers 503 (the workspace's runtime
    does not answer; a retry may work) and not 500. NOTE it is also an ``OSError`` (every ``TimeoutError`` is): a handler that turns an
    ``OSError`` into "the workspace was removed" must let this one through first (``wake_session`` does). A write that FAILS (an
    ``OSError``, a conflict) is not this: it raises as itself and leaves the writer open.
    """

    def __init__(self, message: str) -> None:
        WorkspaceUnreachableError.__init__(self, message)


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
        # The append of the batch last taken out of the buffer, while it is still running (see ``_do_flush``).
        self._write: asyncio.Task[None] | None = None
        # When that append was handed over (monotonic clock), what its backend has reported about it (see ``write_clock``), and which
        # seqs it carries, for the bound and for the log line of a loss.
        self._write_started_at: float = 0.0
        self._write_clock: write_clock.WriteClock | None = None
        self._write_span: tuple[int, int] = (0, 0)
        # The seq of the oldest record in the buffer (0 when empty), for the same log line.
        self._buffer_first_seq: int = 0
        # Set when a batch in flight went unanswered past the bound: the writer starts nothing after that (see ``_do_flush``).
        self._broken: str | None = None

    @property
    def last_seq(self) -> int:
        """The highest seq assigned so far (== ``start_seq`` before any append).

        Callers persist this back to the session row's ``last_seq`` at turn
        boundaries so the next turn's writer (and any concurrent
        ``wake_session``/``reset_session``) seed past this turn's records and
        ``(session_id, seq)`` stays monotonic across turns.
        """
        return self._seq

    async def reserve_seq(self) -> int:
        """Flush what is buffered, then hand out the next seq to a record somebody else writes.

        A compaction marker is written by the executor straight into the log, outside this buffer. If
        it took "the file's next seq" while records the writer has already numbered were still buffered,
        the writer's next record would repeat it (and the marker would sit in the file before events with
        lower seqs). Flushing first keeps the file in seq order; advancing the counter keeps it unique.
        """
        await self._do_flush()
        self._seq += 1
        return self._seq

    async def append(self, record: SessionMessageRecord) -> int:
        """Append a record; flush per buffer policy.

        The writer overwrites ``record.seq`` with its own monotonic counter.

        Returns:
            The assigned seq number (1-based, monotonically increasing).
        """
        self._raise_if_broken()

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

        # Age policy: the oldest buffered record is too old (decided before this one is added).
        age_due = self._oldest_at is not None and time.monotonic() - self._oldest_at >= _FLUSH_AGE_S

        # Buffer the record BEFORE any flush. It already has its seq, so a cancel that lands in the flush below must not
        # be able to drop it (it used to be flushed around, and a cancel there lost it with the seq already spent).
        self._buffer.append(line)
        self._buffer_size += len(line)
        if self._oldest_at is None:
            self._oldest_at = time.monotonic()
            self._buffer_first_seq = assigned_seq

        if age_due or self._buffer_size >= _FLUSH_BYTES:
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
        """Write the current buffer to workspace_io and reset it.

        A batch that has left the buffer IS written, even if the task flushing it is cancelled while it waits: the append
        runs as its own task, and the flushing task awaits it without cancelling it. A cancel (a Stop's cancel of a call, a hard
        Cancel) used to land in that await after the buffer was emptied, so the batch was gone from the buffer and was
        never written; the writer is shared (the delegation recorder writes a subagent's records through the parent's),
        so one cancelled call could drop the parent turn's records too.

        A later flush waits for an append that is still in flight before it takes its own batch, so the file stays in seq
        order. An append that fails still raises in the task that is awaiting it, as before.

        None of those waits is unbounded. A batch the workspace never answers (its runtime connection dropped) used to hold the
        flush that sent it and, through the wait above, every flush after it, so a turn's exit never landed. A flush now waits for a
        batch until it has been in flight ``_WRITE_TIMEOUT_S``, then ABANDONS it: it logs the loss, drops what is buffered behind it,
        BREAKS the writer and raises :class:`WorkspaceWriteTimeout`. A broken writer starts no write after that, and its appends and
        flushes raise at once, which is what keeps the file in order (nothing this writer holds can overtake the abandoned batch)
        and what stops every later flush from waiting the bound again. The abandoned batch is neither cancelled nor re-sent: if the
        workspace answers late it is in the file exactly once, and nothing was queued to be written a second time. The next turn
        builds a new writer, which probes the workspace afresh. A write that FAILS (as opposed to never returning) raises as itself and
        does not break the writer.
        """
        self._raise_if_broken()
        while self._write is not None and not self._write.done():
            await self._wait_for(self._write)            # waiting does not cancel it, and does not raise its error
        if not self._buffer:
            return
        combined = b"".join(self._buffer)
        self._write_span = (self._buffer_first_seq, self._seq)
        self._buffer = []
        self._buffer_size = 0
        self._oldest_at = None
        self._buffer_first_seq = 0
        clock = write_clock.WriteClock()
        token = write_clock.bind(clock)          # the append task copies this context, so its backend can report to the clock
        try:
            write = asyncio.ensure_future(self._io.append_message_line(self._session_id, combined))
        finally:
            write_clock.unbind(token)
        self._write = write
        self._write_clock = clock
        self._write_started_at = time.monotonic()
        _APPENDS.add(write)
        write.add_done_callback(_append_done)
        await self._wait_for(write)
        write.result()                                   # an append that failed raises here, in the task that awaited it

    async def _wait_for(self, write: asyncio.Task[None]) -> None:
        """Wait for ``write`` until its limit (:meth:`_limit`); abandon it and break the writer if it is not done by then.

        Does not cancel ``write`` when the WAITER is cancelled, and does not raise the write's own error (the caller reads it).
        """
        while not write.done():
            remaining = self._limit() - time.monotonic()
            if remaining <= 0:
                self._break_on(write)
                self._raise_if_broken()
            await asyncio.wait({write}, timeout=remaining)         # the limit may have moved (the lock was taken): look again

    def _limit(self) -> float:
        """The monotonic time past which the batch in flight is treated as hung.

        ``_WRITE_TIMEOUT_S`` after the request was SENT, when the backend reports it (the wait for the session's messages_lock, which
        a turn persist holds across its git commit, is not the workspace being dead); and in all at most ``_QUEUE_CAP_FACTOR`` bounds
        after the hand-over, so a lock holder that never lets go cannot hold the batch for ever. A request that was sent only just
        before the cap still gets a quarter of a bound to answer (a healthy slow commit that held the lock for nearly the whole cap
        must not then lose its records). A backend that does not report keeps the clock from the hand-over.
        """
        start = self._write_started_at
        clock = self._write_clock
        if clock is None or not clock.queued:
            return start + _WRITE_TIMEOUT_S
        cap = start + _WRITE_TIMEOUT_S * _QUEUE_CAP_FACTOR
        if clock.sent_at is None:
            return cap
        return max(min(clock.sent_at + _WRITE_TIMEOUT_S, cap), clock.sent_at + _WRITE_TIMEOUT_S / 4)

    def _break_on(self, write: asyncio.Task[None]) -> None:
        """Abandon the unanswered ``write``: log the loss once, drop the buffer, close the writer."""
        if self._broken is not None:
            return                                       # another waiter gave up on it first
        _metrics.message_write_abandoned_total.inc()
        first, last = self._write_span
        dropped = len(self._buffer)
        self._broken = (
            f"the workspace did not accept a batch of message records (seq {first}-{last}) within {_WRITE_TIMEOUT_S:g}s; "
            "the writer is closed"
        )
        logger.warning(
            "session %s: %s; that batch is abandoned (it is never re-sent; if the workspace answers late it lands once) and %d "
            "buffered record(s) behind it are dropped",
            self._session_id, self._broken, dropped,
        )
        self._buffer = []
        self._buffer_size = 0
        self._oldest_at = None
        self._buffer_first_seq = 0
        write.add_done_callback(self._abandoned_write_done)

    def _abandoned_write_done(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is None:
            logger.warning(
                "session %s: the abandoned batch of message records (seq %d-%d) was accepted by the workspace after all; it is in the "
                "log once, after any records a later turn wrote",
                self._session_id, *self._write_span,
            )

    def _raise_if_broken(self) -> None:
        if self._broken is not None:
            raise WorkspaceWriteTimeout(f"session {self._session_id}: {self._broken}")


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
    # 01a0518b (seam-split A-chat/workspace surface): scoped tool-call id ->
    # the durable TOOL_CALL record's own seq. translate_stream_event is
    # synchronous and never sees the seq (WorkspaceMessageWriter.append is
    # async and owned by the caller), so this is populated by the dispatch
    # loop immediately after append() returns, keyed by the SAME scoped id
    # as scoped_call_ids above (== TOOL_CALL payload["id"] ==
    # ToolCallTask.id). The eventual seam-split dispatch reads this map
    # SYNCHRONOUSLY instead of threading a return value back through
    # run_agent_turn's yield chain — safe on this surface because the
    # chat/workspace path is a pull-based async-generator chain with no
    # fan-out queue in between: by the time run_agent_turn's frame resumes
    # after yielding a ToolCallEnd event, the consumer has already run
    # translate_stream_event + awaited append() for it. Never popped —
    # bounded to this _CoalesceState's lifetime (one turn) like every
    # other field here.
    tool_call_record_seq: dict[str, int] = field(default_factory=dict)
    # 01a0518b (seam-split summit): scoped tool-call id -> the durable
    # TOOL_CALL record's own tool name (payload["name"]), populated in the
    # SAME dispatch-loop append site as tool_call_record_seq above. The
    # except-ToolWaitPark branch needs a tool_name to construct each
    # ToolCallTask row (a required field), but ToolWaitPark itself only
    # carries scoped ids (see that exception's own docstring on staying
    # additive-only) - resolving it from the record already written here
    # avoids widening the exception's approved shape for data the
    # append loop already has in hand. NO ENTRY (not a fabricated
    # placeholder string) when the record's own payload["name"] is
    # missing (review fix, 01a0518b): the except-ToolWaitPark branch's
    # ``tool_name is None`` check must see a genuine miss, not an
    # "unknown" string that would silently produce an unexecutable
    # ToolCallTask row.
    tool_call_record_name: dict[str, str] = field(default_factory=dict)
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
    | ExtendedEvent(_CompactionNote)     | COMPACTION_NOTE                |
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
        end_payload: dict[str, Any] = {
            "text": event.text,
            "parsed": event.parsed,
            "end_node_id": event.end_node_id,
        }
        if event.nested:
            # A subgraph's End output forwarded by its parent: kept in the transcript, but not the run's result.
            end_payload["nested"] = True
        return SessionMessageRecord(
            seq=1,  # WorkspaceMessageWriter overwrites
            kind=SessionMessageKind.ASSISTANT_TOKEN,
            payload=end_payload,
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

    if isinstance(event, ExtendedEvent) and isinstance(event.extended, _CompactionNote):
        note = event.extended
        return SessionMessageRecord(
            seq=1,  # WorkspaceMessageWriter overwrites
            kind=SessionMessageKind.COMPACTION_NOTE,
            payload={
                "outcome": note.outcome,
                "reason": note.reason,
                "estimated_tokens": note.estimated_tokens,
                "trigger_tokens": note.trigger_tokens,
            },
            node_id=node_id,
            created_at=now,
        )

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
                **(
                    {"estimated_input_tokens": call.estimated_input_tokens}
                    if call.estimated_input_tokens is not None else {}
                ),
                **(
                    {"cached_input_tokens": call.cached_input_tokens}
                    if call.cached_input_tokens is not None else {}
                ),
                **({"context_length": call.context_length} if call.context_length is not None else {}),
                **({"guard": call.guard} if call.guard != "none" else {}),
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


def flush_partial_output(
    state: "_CoalesceState",
    *,
    delta_sink: "_DeltaSink | None" = None,
    turn_no: int = 0,
) -> list[SessionMessageRecord]:
    """Drain the coalesce buffers into records when a turn is cut short (Stop or Cancel).

    Text and reasoning are coalesced and only become a durable record at a tool call
    (``ToolCallEnd``) or at ``Done``. A turn stopped mid-answer reaches neither, so what
    the model had already streamed lived only in the live tap and vanished on refresh.
    This turns whatever is buffered, for every node, into the same REASONING /
    ASSISTANT_TOKEN records those flush points produce (thought before answer, as there),
    and closes the live parts so the client stops showing them as still streaming. The
    caller appends the records, then the CANCELLED record that explains why they end there.

    ``last_assistant_token`` is deliberately NOT set: it feeds the final-result relay of a
    turn that completed, and a stopped turn has no final result.
    """
    now = _now_utc()
    records: list[SessionMessageRecord] = []
    node_ids = list(dict.fromkeys([*state.reasoning_buffers, *state.text_buffers]))
    for node_id in node_ids:
        thought = state.reasoning_buffers.pop(node_id, "")
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
        buffered = state.text_buffers.pop(node_id, "")
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
        if delta_sink is not None:
            delta_sink.close(part_id(node_id, KIND_TEXT, turn_no))
            delta_sink.close(part_id(node_id, KIND_REASONING, turn_no))
    return records


async def stash_and_flush_tool_call_record(
    rec: SessionMessageRecord,
    seq: int,
    *,
    coalesce_state: "_CoalesceState",
    writer: WorkspaceMessageWriter,
    tool_calls_as_claims_enabled: bool,
) -> None:
    """The tool-dispatch seam's per-TOOL_CALL-record durability step
    (Phase 3 stage 7a, 01a0518b; 7a gate review item 3 - extracted as a
    SHARED helper so the live turn loop (``primer.session.dispatch``)
    and a graph resume drain (``primer.worker.graph_resume.
    _ResumeDrainTap``) cannot drift into two independently-maintained
    copies of the same seam - the exact "pending_dispatch disease" this
    arc has repeatedly guarded against elsewhere).

    A no-op unless BOTH ``rec.kind == TOOL_CALL`` and
    ``tool_calls_as_claims_enabled`` - see 7a gate review item A: neither
    the stash nor the flush serves any purpose when the flag is off
    (nothing on that path can ever produce a ``ToolWaitPark`` to read the
    stash back, and there is no different-process reader to make
    durable for).

    Stashes the record's own ``seq`` into ``coalesce_state.
    tool_call_record_seq`` (keyed by the record's own id - the tool-
    dispatch seam's ``except ToolWaitPark`` row-creation reads this
    synchronously) and its ``name`` into ``tool_call_record_name`` - no
    ``"or 'unknown'"`` fallback: a missing name must surface as a
    MISSING dict entry, not a fabricated string, so that row-creation's
    own ``tool_name is None`` invariant check fails loudly instead of
    producing an unexecutable ``ToolCallTask``. Then flushes ``writer``
    immediately, bypassing its normal 16KB/100ms buffered policy - a
    claim-based worker reads ``messages.jsonl`` from a DIFFERENT
    PROCESS once the seam is armed, so unflushed bytes would be
    invisible to it; durable means flushed, always, for this one record
    kind, on this one path.
    """
    if rec.kind != SessionMessageKind.TOOL_CALL or not tool_calls_as_claims_enabled:
        return
    coalesce_state.tool_call_record_seq[rec.payload["id"]] = seq
    tool_call_name = rec.payload.get("name")
    if tool_call_name is not None:
        coalesce_state.tool_call_record_name[rec.payload["id"]] = tool_call_name
    await writer.flush()


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


class TurnInvariantError(RuntimeError):
    """A deterministic bookkeeping invariant of the turn broke: running it again cannot help, so it must end failed.

    Raised when the state the turn reads proves it (a row of another session holds the id, a parked call has no
    durable TOOL_CALL record, a row of this session holds the id under a different record_seq), so a retry would
    run into the same state. The live-turn park arms of ``primer.session.dispatch`` end the session failed on it.
    It is NOT for transient storage errors: those keep propagating as themselves and do not end the session. A
    ``RuntimeError``, so a caller that catches that keeps working.
    """


async def _create_tool_call_task_idempotent(
    task_storage, task: "Any", *, session_id: str, strict: bool = True,
) -> None:
    """Create ``task``, tolerating a crash-and-retry replay of the SAME
    scoped id (01a0518b review: the crash-window doctrine).

    Crash window: a worker can crash AFTER a park branch has created
    every ``ToolCallTask`` row (+ upserted the claimable ones' leases)
    but BEFORE the ``ParkRequest`` this turn returns is ever applied by
    ``on_release`` (that write happens in the CALLER, one layer above).
    The session's lease then simply expires (never released) and the
    turn re-runs from scratch.

    That re-run mints the IDENTICAL scoped ids and record_seq values
    as the crashed attempt, deterministically, for two independent
    reasons: (1) the seed seq is read from ``session.last_seq``, which
    the crashed attempt never advanced (the durable append only
    completes AFTER this) -- so the re-run's fresh writer starts
    counting from the SAME base and produces the SAME seq for the same
    append order; (2) a park never reaches the normal turn-persist step
    (the assistant/tool_use message lives only in the exception's
    ``llm_messages``, lost with the crash), so the re-run's LLM prompt
    is byte-identical to the crashed attempt's -- if the LLM replays the
    same tool calls in the same order (the expected case), a fresh
    coalesce state mints identical scoped ids from turn_no + positional
    seq alone.

    So a ``ConflictError`` here on retry, with the existing row's
    ``record_seq`` matching what THIS attempt just computed, is proof
    of exactly that replay -- not a real collision -- and is treated as
    a no-op. A MISMATCHED ``record_seq`` means something else entirely
    created this id (the LLM did not replay identically, or a genuine
    bug), which is not safe to paper over: raise loudly rather than
    silently resurrecting or overwriting scheduling state a worker
    might already be running against.

    This doctrine only covers a BYTE-IDENTICAL replay. If the re-run's
    LLM instead emits a genuinely DIFFERENT batch, the crashed
    attempt's OWN rows (never referenced by the new attempt at all)
    become orphans -- see
    ``primer.worker.tool_wait_resume_coordinator``'s module docstring
    for the required liveness check the future claim-based tool-call
    worker must perform before executing one.

    7a gate review (verdict R3-3): the "record_seq must match, else it's
    not a real replay" half of this doctrine is FALSE at the graph-resume
    repark call site (``strict=False``). Its precondition - "the seed seq
    is read from session.last_seq, which the crashed attempt never
    advanced" - does not hold there: on that chain,
    ``persist_resume_tool_result_record_for_graph`` durably advances
    ``last_seq`` BEFORE the drain runs, and a crash-then-retry re-enters
    the SAME drain, so the retry's own record_seq for the SAME scoped id
    is legitimately DIFFERENT from the first attempt's - not evidence of
    "something else created a conflicting row". Comparing record_seq
    there would turn every crash-retry into a raised RuntimeError, which
    the caller maps to ``_end_session(failed)`` - a crash-retry KILLS the
    session instead of replaying it. ``strict=False`` therefore treats
    ANY existing row for this scoped id as a replay without comparing
    record_seq at all (logged at debug, not raised); ``strict=True``
    (the live-turn park catches, where the precondition above genuinely
    holds) keeps the loud mismatch-raises-loudly behavior unchanged.
    """
    from primer.model.except_ import ConflictError

    try:
        await task_storage.create(task)
    except ConflictError:
        existing = await task_storage.get(task.id)
        if existing is not None and existing.session_id != task.session_id:
            # Row ids are session-qualified (S1b), so this should be unreachable; if a row of ANOTHER session holds
            # this id, adopting it would hand this session that session's result. Never a replay, whatever the seq.
            raise TurnInvariantError(
                f"session {session_id} ToolCallTask {task.id!r} already exists for session "
                f"{existing.session_id!r}: a cross-session id collision, not a crash-retry replay"
            ) from None
        if existing is not None and existing.record_seq == task.record_seq:
            logger.info(
                "session %s ToolCallTask %r already exists with matching "
                "record_seq %d - crash-and-retry replay, treating as a "
                "no-op",
                session_id, task.id, task.record_seq,
            )
            return
        if not strict and existing is not None:
            logger.debug(
                "session %s ToolCallTask %r already exists with "
                "record_seq=%d (this attempt computed %d) - treated as a "
                "repark replay without record_seq comparison (strict=False "
                "call site, see this function's own docstring on why "
                "record_seq legitimately differs there)",
                session_id, task.id, existing.record_seq, task.record_seq,
            )
            return
        # Deterministic while the row is there: it is still there when the turn runs again, and a re-run gets past it
        # only with a record_seq that matches by chance, the adoption this doctrine refuses. A row that refused the
        # create but is gone on read lost a race with a delete, which is no reason to end the turn.
        error = RuntimeError if existing is None else TurnInvariantError
        raise error(
            f"session {session_id} ToolCallTask {task.id!r} already exists "
            "with record_seq="
            f"{existing.record_seq if existing is not None else '<gone>'} "
            f"but this attempt computed record_seq={task.record_seq} - not "
            "a crash-retry replay (record_seq must match for that); "
            "something else created a conflicting row"
        ) from None


async def materialize_pending_tool_wait_rows(
    storage_provider: "Any",
    claim_engine: "Any | None",
    session_id: str,
    turn_no: int,
    coalesce_state: "_CoalesceState",
    parked_at: datetime,
    pending_tool_waits: "list[dict[str, Any]]",
    *,
    strict: bool = True,
) -> list[str]:
    """Materialize ``ToolCallTask`` rows for every co-pending tool_wait
    batch found in a graph checkpoint's own ``pending_tool_waits`` list
    (01a0518b, graph-surface boundary d) - one independent batch per
    graph node that raised its own ``ToolWaitPark`` in the same
    superstep. Returns each batch's own wake key (see
    ``tool_wait_event_key``), for the caller to fold into its own
    ``event_keys``/``parked_event_keys``. A batch whose task id does not
    parse gets no key (logged at ERROR and counted, never guessed) and is
    left out of the list; its rows are still created. The caller decides
    what an empty list means: the mixed park still has its human gate's
    key, a pure tool_wait park arm ends the turn failed.

    7a gate review (verdict R2-1): relocated here (from
    ``primer.session.dispatch``, where it was a private helper) so BOTH
    dispatch.py's live-turn park catches AND the graph-resume adapter's
    own repark path (``primer.worker.graph_resume.
    resume_graph_from_checkpoint``) share ONE implementation - a
    resumed node's own continuation dispatching a SECOND claims batch
    re-parks via a repark path that, before this fix, created NO rows
    at all for that new batch (the "pure re-write, no new rows" comment
    it carried was true only because the claims seam was structurally
    unreachable on resume before item 3's fix; item 3 made it reachable
    without this row-creation half following it over).

    ``strict`` distinguishes the two call shapes: dispatch.py's ORIGINAL
    park catches pass the FULL, freshly-produced ``pending_tool_waits``
    list, where every entry's scoped ids MUST already have a durable
    TOOL_CALL record in ``coalesce_state`` (a miss there is a genuine
    invariant violation - keep raising loudly, ``strict=True``, the
    default). The repark path's ``pending_tool_waits`` can be a MIX of
    entries carried over untouched from a PRIOR park (this resume's own
    ``coalesce_state`` is fresh and never observed them) and genuinely
    NEW entries this resume's own dispatch just produced (which DOES
    know about them) - pass ``strict=False`` there so an untouched
    entry's scoped ids are silently skipped (already materialized
    earlier, not this call's business) rather than raising.

    Shared by BOTH graph park paths: the classic ``except YieldToWorker``
    branch (a co-pending human gate rides alongside one or more tool_wait
    batches) and the pure ``except ToolWaitPark`` branch (no co-pending
    gate) both read this SAME ``graph_checkpoint['pending_tool_waits']``
    shape, rather than the pure branch's own FLATTENED
    ``ToolWaitPark.outstanding_task_ids``/``notifying_results`` fields -
    flattening across nodes would lose the per-node grouping
    ``batch_task_ids``/the wake key both need to keep "one gated call's
    siblings don't block another node's siblings" true for tool_wait
    batches too, not just human gates (see ``_PendingToolWait``'s own
    docstring). ``[]`` for an agent-bound park, or a graph park with no
    co-pending tool_wait batch at all - a no-op.
    """
    from primer.int.claim import CLAIM_PRIORITY_RESUME, ClaimKind
    from primer.model.tool_call_task import (
        ToolCallTask,
        ToolCallTaskState,
        external_call_id,
        tool_call_task_id,
    )
    from primer.session.yields import tool_wait_event_key_or_none

    if not pending_tool_waits:
        return []
    task_storage = storage_provider.get_storage(ToolCallTask)
    wake_keys: list[str] = []
    for pw in pending_tool_waits:
        skipped: set[str] = set()   # ids of this entry whose rows an EARLIER park created (non-strict only)
        # An entry's ids are SCOPED when this park produced it and already QUALIFIED when it is carried over from an
        # earlier park (the checkpoint stores the qualified form). The transcript records are keyed on the scoped id,
        # the rows, leases and batch lists on the qualified one; both forms normalise here.
        call_ids: dict[str, str] = pw.get("call_ids") or {}
        outstanding_scoped = [
            external_call_id(i, session_id) for i in pw["outstanding_task_ids"]
        ]
        notifying_scoped = [
            external_call_id(i, session_id) for i, _ in pw["notifying_results"]
        ]
        node_batch_ids = [
            tool_call_task_id(session_id, i)
            for i in [*outstanding_scoped, *notifying_scoped]
        ]
        for scoped_id in outstanding_scoped:
            record_seq = coalesce_state.tool_call_record_seq.get(scoped_id)
            tool_name = coalesce_state.tool_call_record_name.get(scoped_id)
            if record_seq is None or tool_name is None:
                if not strict:
                    skipped.add(scoped_id)
                    continue
                raise TurnInvariantError(
                    f"session {session_id} pending tool_wait node "
                    f"{pw['node_id']!r} outstanding task {scoped_id!r} has "
                    "no matching TOOL_CALL record in this turn's "
                    "coalesce_state - the durable-append-before-claimable "
                    "invariant broke"
                )
            await _create_tool_call_task_idempotent(
                task_storage,
                ToolCallTask(
                    id=tool_call_task_id(session_id, scoped_id),
                    session_id=session_id,
                    turn_no=turn_no,
                    tool_name=tool_name,
                    state=ToolCallTaskState.QUEUED,
                    record_seq=record_seq,
                    call_id=call_ids.get(scoped_id),
                    created_at=parked_at,
                    batch_task_ids=node_batch_ids,
                ),
                session_id=session_id,
                strict=strict,
            )
            if claim_engine is not None:
                await claim_engine.upsert(
                    ClaimKind.TOOL_CALL, tool_call_task_id(session_id, scoped_id),
                    priority=CLAIM_PRIORITY_RESUME,
                )
        for notifying_id, result_dict in pw["notifying_results"]:
            scoped_id = external_call_id(notifying_id, session_id)
            record_seq = coalesce_state.tool_call_record_seq.get(scoped_id)
            tool_name = coalesce_state.tool_call_record_name.get(scoped_id)
            if record_seq is None or tool_name is None:
                if not strict:
                    skipped.add(scoped_id)
                    continue
                raise TurnInvariantError(
                    f"session {session_id} pending tool_wait node "
                    f"{pw['node_id']!r} notifying result {scoped_id!r} has "
                    "no matching TOOL_CALL record in this turn's "
                    "coalesce_state - the durable-append-before-claimable "
                    "invariant broke"
                )
            await _create_tool_call_task_idempotent(
                task_storage,
                ToolCallTask(
                    id=tool_call_task_id(session_id, scoped_id),
                    session_id=session_id,
                    turn_no=turn_no,
                    tool_name=tool_name,
                    state=ToolCallTaskState.DONE,
                    record_seq=record_seq,
                    call_id=call_ids.get(scoped_id),
                    created_at=parked_at,
                    finished_at=parked_at,
                    result_state=dict(result_dict),
                    batch_task_ids=node_batch_ids,
                ),
                session_id=session_id,
                strict=strict,
            )
        # Store the QUALIFIED form in the entry itself (it is the dict inside the checkpoint that is parked), so the
        # blob, the rows and the leases agree and every later reader looks the rows up by the id it finds there.
        # An id this call SKIPPED (its rows were created by an earlier park) keeps the form it was stored in: a
        # park written before ids were qualified (a development flag-on park) has its rows under the BARE id, and
        # rewriting the entry to a qualified id the rows do not have would make the next wake find nothing.
        def _stored(original: str, scoped: str) -> str:
            return original if (scoped in skipped and original == scoped) else tool_call_task_id(session_id, scoped)

        pw["outstanding_task_ids"] = [
            _stored(original, scoped)
            for original, scoped in zip(pw["outstanding_task_ids"], outstanding_scoped)
        ]
        pw["notifying_results"] = [
            (_stored(original, external_call_id(original, session_id)), r)
            for original, r in pw["notifying_results"]
        ]
        wake_key = tool_wait_event_key_or_none(session_id, scoped_task_id=node_batch_ids[0], site="materializer")
        if wake_key is not None:
            wake_keys.append(wake_key)
    return wake_keys


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
    "WorkspaceMessageWriter", "WorkspaceWriteTimeout", "WorkspaceIO", "_CoalesceState", "configure_write_timeout",
    "translate_stream_event", "stash_graph_scoped_ids",
]
