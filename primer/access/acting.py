"""The acting person: who the current request, listing or tool call acts for (access-control plan P5 Task 1; spec 6.3, 6.4).

One ContextVar, set by the auth middleware for every authenticated request (REST, WebSocket, MCP: the user, the owner of a bearer token, the system
person when auth is disabled) and by the tool manager around every listing and call of a run, from the run's own ``initiated_by``, so a task that
inherited a request's person never leaks it into a run. Two readers: the MCP OAuth token pick (through
:func:`primer.access.person.person_for_tool` with no ``ToolContext``) and the event writers, which label an event with :func:`actor_label`.

A leaf module: it imports nothing from primer at runtime, so the storage layer can read it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from primer.access.person import Person

_ACTING: ContextVar[Person | None] = ContextVar("primer_acting_person", default=None)


@contextmanager
def acting_as(person: Person | None) -> Iterator[None]:
    """Act as ``person`` inside the block (``None``: nobody); the previous person is back on exit, whatever happens inside."""
    token = _ACTING.set(person)
    try:
        yield
    finally:
        _ACTING.reset(token)


def acting_person() -> Person | None:
    """The person the current code acts for, or ``None`` when nobody is acting (worker internals, trigger dispatch, the dispatcher)."""
    return _ACTING.get()


def actor_label(person: Person | None) -> str:
    """The event actor for ``person``: ``user:<user id>``, else ``system`` (the system person, or nobody acting)."""
    if person is not None and person.user_id:
        return f"user:{person.user_id}"
    return "system"


__all__ = ["acting_as", "acting_person", "actor_label"]
