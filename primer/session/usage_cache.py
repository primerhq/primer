"""A small bounded cache for a session's usage totals (architecture review A-05).

The usage of a session is a pure function of its log (``primer.session.usage.session_usage``), and the session detail route folded the
whole log on every read. The route keys this cache on the identity of the log (every segment's name, size and modification time as the
workspace lists them, and the row's ``last_seq`` and ``turn_no``), so an entry is served only for a log that has not changed. Process
local and bounded: the entries are a handful of integers, the bound is what keeps a long-running API from keeping one per session it ever served.
"""

from __future__ import annotations

import copy
from collections import OrderedDict
from collections.abc import Hashable

from primer.model.except_ import NotFoundError
from primer.session.usage import SessionUsage, UsageFold, session_usage
from primer.workspace.segments import SegmentInfo, SessionLog

__all__ = ["UsageCache", "fold_usage"]

#: How many bytes before the folded end a continued read starts at, to tell that the log still begins with what was folded.
_GUARD_BYTES = 256


class UsageCache:
    def __init__(self, max_entries: int = 512) -> None:
        self._max = max_entries
        self._entries: OrderedDict[Hashable, dict[str, int]] = OrderedDict()
        # The sidecar: per session, the fold of the log up to some byte (None: that log has a rewind marker, which cannot be continued).
        self._folds: OrderedDict[Hashable, UsageFold | None] = OrderedDict()

    def get(self, key: Hashable) -> dict[str, int] | None:
        """A copy of the cached usage for ``key``, or ``None``; a hit makes the entry the most recently used."""
        found = self._entries.get(key)
        if found is None:
            return None
        self._entries.move_to_end(key)
        return dict(found)

    def put(self, key: Hashable, usage: dict[str, int]) -> None:
        self._entries[key] = dict(usage)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        self._entries.clear()
        self._folds.clear()

    def get_fold(self, key: Hashable) -> tuple[bool, UsageFold | None]:
        """``(known, fold)``: ``known`` is False for a session never folded; a known ``None`` fold is a log that must be folded whole."""
        if key not in self._folds:
            return False, None
        self._folds.move_to_end(key)
        return True, self._folds[key]

    def put_fold(self, key: Hashable, fold: UsageFold | None) -> None:
        self._folds[key] = fold
        self._folds.move_to_end(key)
        while len(self._folds) > self._max:
            self._folds.popitem(last=False)


async def fold_usage(log: SessionLog, cache: UsageCache, key: Hashable, *, segments: list[SegmentInfo] | None = None) -> SessionUsage:
    """The usage of the session ``log``, folding only what was appended since the last call for ``key``.

    The fold of the lines already seen is kept in ``cache`` with the LOGICAL offset it reached (into the concatenation of the segments, so it
    stays valid across a rotation) and the last bytes before it; a later call reads from there (``log.read_from``), checks that those bytes are
    still what it folded (a rewrite or trim makes them differ: the log is then folded whole), and folds the complete new lines. A trailing line
    with no newline yet is counted in the answer but not consumed. A log with a rewind marker is folded whole each time (``session_usage``), as
    before. ``segments`` is a listing the caller just made. Raises ``NotFoundError`` for a log with no segment.
    """
    segments = segments if segments is not None else await log.segments()
    if not segments:
        raise NotFoundError("the session has no log yet")

    async def read_from(offset: int) -> bytes:
        return await log.read_from(offset, segments=segments)

    known, fold = cache.get_fold(key)
    fresh: bytes | None = None
    if known and fold is None:
        return _whole(await read_from(0))
    if fold is not None:
        start = max(0, fold.consumed - len(fold.guard))
        tail = await read_from(start)
        if tail[:len(fold.guard)] == fold.guard:
            fresh = tail[len(fold.guard):]
        else:
            fold = None
    if fresh is None:
        fold, fresh = UsageFold(), await read_from(0)
    cut = fresh.rfind(b"\n") + 1
    complete, partial = fresh[:cut], fresh[cut:]
    if not fold.feed(complete.decode("utf-8", errors="replace").splitlines()):
        cache.put_fold(key, None)
        return _whole(await read_from(0))
    fold.consumed += len(complete)
    fold.guard = (fold.guard + complete)[-_GUARD_BYTES:]
    cache.put_fold(key, fold)
    if not partial:
        return fold.usage()
    trial = copy.deepcopy(fold)
    trial.feed(partial.decode("utf-8", errors="replace").splitlines())
    return trial.usage()


def _whole(raw: bytes) -> SessionUsage:
    return session_usage(raw.decode("utf-8", errors="replace").splitlines())
