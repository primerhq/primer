"""A memo of a pure, costly function of an object, keyed by the object's identity.

The per-turn token estimates run over the same message parts and tool definitions every round of a turn; serialising a tool schema or a call's
arguments each time is the cost this removes. The entry holds a weak reference (an id is only trusted while the object it was taken from is alive,
and the entry goes with it) and a cheap ``guard`` value, so a part or schema replaced or resized in place is recomputed, not served stale.
"""

from __future__ import annotations

import weakref
from collections.abc import Callable, Hashable
from typing import Any, TypeVar

T = TypeVar("T")


class IdentityMemo:
    def __init__(self) -> None:
        self._entries: dict[int, tuple[weakref.ref[Any], Hashable, Any]] = {}

    def get(self, obj: Any, guard: Hashable, compute: Callable[[], T]) -> T:
        oid = id(obj)
        entry = self._entries.get(oid)
        if entry is not None and entry[0]() is obj and entry[1] == guard:
            return entry[2]
        value = compute()
        try:
            ref = weakref.ref(obj, lambda _ref, oid=oid: self._entries.pop(oid, None))
        except TypeError:  # not weak-referenceable: nothing safe to key on
            return value
        self._entries[oid] = (ref, guard, value)
        return value

    def __len__(self) -> int:
        return len(self._entries)
