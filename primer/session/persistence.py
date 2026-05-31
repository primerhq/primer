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

from primer.model.chat import (
    Done,
    Error,
    ExtendedEvent,
    StreamEvent,
    TextDelta,
    ToolCallEnd,
    _ExecutorToolResult,
)
from primer.model.workspace_session import SessionMessageKind, SessionMessageRecord

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


class WorkspaceMessageWriter:
    """Buffered jsonl appender for session messages.

    Buffers up to 100 ms or 16 KB to amortise workspace I/O cost.
    Tick events fire per-record (not per-flush) so live WS subscribers
    see real-time deltas.

    Args:
        workspace_io: Dependency satisfying :class:`WorkspaceIO`.
        session_id: Identifies the workspace session being written.
    """

    def __init__(self, *, workspace_io: WorkspaceIO, session_id: str) -> None:
        self._io = workspace_io
        self._session_id = session_id
        self._seq: int = 0

        # Buffer state
        self._buffer: list[bytes] = []
        self._buffer_size: int = 0
        self._oldest_at: float | None = None  # monotonic clock at first buffered record

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
        # Future: publish to SessionTickRouter here.

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
    coalesce into a single assistant_token record on Done/ToolCallEnd."""

    text_buffer: str = field(default="")


def translate_stream_event(
    event: StreamEvent,
    state: _CoalesceState,
) -> "SessionMessageRecord | list[SessionMessageRecord] | None":
    """Per-event translation following the chat-selective persistence cadence.

    | Event                | Output                                          |
    |----------------------|-------------------------------------------------|
    | TextDelta            | None (coalesces into state.text_buffer)         |
    | ToolCallEnd          | flush text_buffer (if any), then TOOL_CALL      |
    | ExtendedEvent(_ExecutorToolResult) | TOOL_RESULT                    |
    | Done                 | flush text_buffer (if any), then DONE           |
    | Error                | ERROR                                           |
    | _GraphErrorEvent     | ERROR (graph runtime terminal failure)          |
    | (others)             | None — silently dropped                         |

    Worker code is responsible for synthetic kinds (USER_INPUT, CANCELLED,
    YIELDED, RESUMED) — not produced by this translator from LLM events.
    """
    now = _now_utc()

    # Graph runtime terminal-failure event (spec §5.4). Imported locally
    # to avoid a hard import-time dependency from primer.session on
    # primer.graph (the latter brings in jinja2 + jsonschema, which the
    # agent-only session path doesn't need).
    from primer.graph.base import _GraphErrorEvent

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
            created_at=now,
        )

    if isinstance(event, TextDelta):
        state.text_buffer += event.text
        return None

    if isinstance(event, ToolCallEnd):
        records: list[SessionMessageRecord] = []
        if state.text_buffer:
            records.append(
                SessionMessageRecord(
                    seq=1,
                    kind=SessionMessageKind.ASSISTANT_TOKEN,
                    payload={"text": state.text_buffer},
                    created_at=now,
                )
            )
            state.text_buffer = ""
        records.append(
            SessionMessageRecord(
                seq=1,
                kind=SessionMessageKind.TOOL_CALL,
                payload={"id": event.id, "arguments": event.arguments},
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
            created_at=now,
        )

    if isinstance(event, Done):
        records = []
        if state.text_buffer:
            records.append(
                SessionMessageRecord(
                    seq=1,
                    kind=SessionMessageKind.ASSISTANT_TOKEN,
                    payload={"text": state.text_buffer},
                    created_at=now,
                )
            )
            state.text_buffer = ""
        done_record = SessionMessageRecord(
            seq=1,
            kind=SessionMessageKind.DONE,
            payload={"stop_reason": event.stop_reason, "raw_reason": event.raw_reason},
            created_at=now,
        )
        if records:
            records.append(done_record)
            return records
        return done_record

    if isinstance(event, Error):
        return SessionMessageRecord(
            seq=1,
            kind=SessionMessageKind.ERROR,
            payload={"message": event.message, "code": event.code, "fatal": event.fatal},
            created_at=now,
        )

    # All other events (StreamStart, ReasoningDelta, ToolCallStart, ToolCallDelta,
    # MediaDelta, Usage, ExtendedEvent without _ExecutorToolResult) — silently dropped.
    return None


__all__ = ["WorkspaceMessageWriter", "WorkspaceIO", "_CoalesceState", "translate_stream_event"]
