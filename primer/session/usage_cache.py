"""A small bounded cache for a session's usage totals (architecture review A-05).

The usage of a session is a pure function of its ``messages.jsonl`` (``primer.session.usage.session_usage``), and the session detail route
folded the whole log on every read. The route keys this cache on the identity of the log (the file's size and modification time as the
workspace reports them, and the row's ``last_seq`` and ``turn_no``), so an entry is served only for a log that has not changed. Process
local and bounded: the entries are a handful of integers, the bound is what keeps a long-running API from keeping one per session it ever served.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Hashable

__all__ = ["UsageCache"]


class UsageCache:
    def __init__(self, max_entries: int = 512) -> None:
        self._max = max_entries
        self._entries: OrderedDict[Hashable, dict[str, int]] = OrderedDict()

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
