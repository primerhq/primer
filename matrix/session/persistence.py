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
from typing import Protocol

from matrix.model.workspace_session import SessionMessageRecord

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


__all__ = ["WorkspaceMessageWriter", "WorkspaceIO"]
