"""Where a NEW event subscriber starts reading (GS-34).

``store.max_id()`` is the highest id that has committed, but on Postgres an id is taken at INSERT, so a transaction that holds a lower id may
still be running: a cursor seeded at ``max_id()`` is then ABOVE an event that commits a moment later, and the event is never delivered. The
dispatcher already refuses to move a cursor past such a hole (``EventDispatcher._gap_resolved``); a cursor must be seeded by the same rule.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from primer.events.dispatcher import HOLE_MAX_WAIT_SECONDS

if TYPE_CHECKING:
    from primer.int.event_store import EventStore

# Gaps further back than this many events are never searched: a transaction that old has finished long since.
_WINDOW = 1000


async def stable_head(store: "EventStore") -> int:
    """The highest event id ``H`` such that no event with an id at or below ``H`` can still appear: the cursor a new subscriber starts from.

    ``max_id()`` when the store has no commit-order hole (SQLite, memory), when no transaction is running, or when the gaps are all old (the
    dispatcher passes a gap after ``HOLE_MAX_WAIT_SECONDS`` itself). Otherwise the id just below the first recent gap: an event above the gap that
    committed before the call can then be seen again, which beats missing the one that commits after it. An unreadable horizon answers ``max_id()``.
    """
    head = await store.max_id()
    horizon = await store.read_horizon()
    if head == 0 or horizon is None or horizon[0] >= horizon[1]:
        return head
    floor = max(head - _WINDOW, 0)
    previous = floor
    now = datetime.now(UTC)
    for event in await store.read_after(floor, limit=_WINDOW + 1):
        if event.id > previous + 1 and (now - event.occurred_at).total_seconds() < HOLE_MAX_WAIT_SECONDS:
            return previous
        previous = event.id
    return head
