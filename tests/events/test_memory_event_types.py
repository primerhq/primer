"""The memory.* action event types are in the closed catalog (agent memory Task 3)."""

from primer.events.catalog import ACTION_EVENT_TYPES, is_known_event_type

MEMORY_EVENTS = (
    "memory.entry.added", "memory.entry.updated", "memory.entry.promoted", "memory.entry.approved", "memory.entry.archived",
    "memory.reflection.completed", "memory.reflection.skipped", "memory.lookup", "memory.curated", "memory.purged",
)


def test_memory_event_types_are_in_the_catalog() -> None:
    assert set(MEMORY_EVENTS) <= ACTION_EVENT_TYPES and all(is_known_event_type(t) for t in MEMORY_EVENTS)
