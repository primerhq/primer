"""A session's reading of its own ``messages.jsonl``, parsed once and continued (Task 14, H2, stage 1).

``messages.jsonl`` is append-only, and several readers in one turn want something from it: the history (marker folded), the newest compaction's state,
the highest event-log seq, the messages since the last answer. Each used to read the whole file and parse every line again. A :class:`MessageLogCache`
(one per session, held by the :class:`~primer.workspace.session.AgentSession`) keeps the folds of what it has read and continues them with only the
bytes appended since:

* the file is read at all only when its ``(size, mtime)`` changed, where the backend can say (a local state repo can; a sandbox cannot and is read
  each time, which still skips the parse);
* a read that no longer starts with the bytes the cache consumed (the file shrank, or anything before the offset moved: another writer, a rewrite)
  discards the cache and parses everything, so a cache never serves what the file no longer says;
* only whole lines are folded: a file that does not end in a newline is parsed in full for that call and the cache is left alone;
* with ``verify`` on (tests, canary: ``WorkerConfig.session_log_verify_reads`` or ``PRIMER_SESSION_LOG_VERIFY_READS=1``) every cached answer is compared
  with a full parse and a mismatch raises.

Decoding and parsing run off the event loop.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from primer.model.chat import Message
from primer.workspace.session import CompactionFold, HistoryFold, LastCompaction, last_compaction_state, reconstruct_compacted_history

Reader = Callable[[], Awaitable[bytes | None]]
Stat = Callable[[], Awaitable[tuple[int, int] | None]]


class MessageLogMismatch(AssertionError):
    """Verify mode: a cached read differs from a full parse of the same bytes."""


@dataclass(frozen=True)
class LogView:
    """What the log said when it was last read."""

    history: list[Message]
    compaction: LastCompaction
    max_seq: int
    size: int


_verify_configured = False
_append_only_configured = False


def set_append_only_default(on: bool) -> None:
    """``WorkerConfig.session_log_append_only``: write a session's log by appending the new lines (Task 14 stage 2); default OFF."""
    global _append_only_configured
    _append_only_configured = bool(on)


def append_only_default() -> bool:
    """The configured flag, or ``PRIMER_SESSION_LOG_APPEND_ONLY=1`` (how a whole test suite is run append-only)."""
    return _append_only_configured or os.environ.get("PRIMER_SESSION_LOG_APPEND_ONLY", "") not in ("", "0", "false", "False")


def set_verify_default(on: bool) -> None:
    """``WorkerConfig.session_log_verify_reads``: verify every cached read of every session (the canary setting)."""
    global _verify_configured
    _verify_configured = bool(on)


def verify_default() -> bool:
    return _verify_configured or os.environ.get("PRIMER_SESSION_LOG_VERIFY_READS", "") not in ("", "0", "false", "False")


class MessageLogCache:
    def __init__(self, *, verify: bool | None = None) -> None:
        self.verify = verify_default() if verify is None else verify
        self._lock = asyncio.Lock()
        self._reset()
        self.reads = 0          # reads of the file (counters for the perf tests)
        self.parsed_bytes = 0   # bytes handed to a parser

    def _reset(self) -> None:
        self._raw = b""
        self._consumed = 0
        self._stat: tuple[int, int] | None = None
        self._history = HistoryFold()
        self._compaction = CompactionFold()

    def invalidate(self) -> None:
        self._reset()

    async def view(self, read: Reader, stat: Stat | None = None) -> LogView:
        async with self._lock:
            return await self._view(read, stat)

    async def _view(self, read: Reader, stat: Stat | None) -> LogView:
        current = await stat() if stat is not None else None
        if current is not None and current == self._stat and self._consumed == len(self._raw):
            return self._snapshot()
        self.reads += 1
        raw = await read() or b""
        view = await asyncio.to_thread(self._advance, raw, current)
        if self.verify:
            await asyncio.to_thread(self._check, raw, view)
        return view

    def _advance(self, raw: bytes, stat: tuple[int, int] | None) -> LogView:
        if not raw.endswith(b"\n") and raw:
            # A partial last line: answer for the whole text without keeping any of it (the line may still be growing).
            return self._full(raw)
        if not raw.startswith(self._raw[: self._consumed]) or len(raw) < self._consumed:
            self._reset()
        tail = raw[self._consumed:]
        if tail:
            lines = tail.decode("utf-8").splitlines()
            self.parsed_bytes += len(tail)
            self._history.feed(lines)
            self._compaction.feed(lines)
            self._raw, self._consumed = raw, len(raw)
        self._stat = stat
        return self._snapshot()

    def _snapshot(self) -> LogView:
        return LogView(
            history=self._history.messages(), compaction=self._compaction.state(), max_seq=self._compaction.max_seq, size=self._consumed,
        )

    @staticmethod
    def _full(raw: bytes) -> LogView:
        lines = raw.decode("utf-8").splitlines()
        history, compaction = HistoryFold(), CompactionFold()
        history.feed(lines)
        compaction.feed(lines)
        return LogView(history=history.messages(), compaction=compaction.state(), max_seq=compaction.max_seq, size=len(raw))

    def _check(self, raw: bytes, view: LogView) -> None:
        lines = raw.decode("utf-8").splitlines()
        want = reconstruct_compacted_history(lines)
        if [m.model_dump(mode="json") for m in view.history] != [m.model_dump(mode="json") for m in want]:
            raise MessageLogMismatch("the cached history differs from a full parse of messages.jsonl")
        if view.compaction != last_compaction_state(lines):
            raise MessageLogMismatch("the cached compaction state differs from a full parse of messages.jsonl")
        full = self._full(raw)
        if view.max_seq != full.max_seq:
            raise MessageLogMismatch("the cached highest event-log seq differs from a full parse of messages.jsonl")


__all__ = ["LogView", "MessageLogCache", "MessageLogMismatch", "append_only_default", "set_append_only_default", "set_verify_default", "verify_default"]
