"""Closed catalog of non-CRUD action event types.

CRUD events (``<kind>.created/updated/deleted``) derive from the kind
registry and are emitted by the storage layer; everything else an
action can emit is named here. The recorder rejects unlisted types, so
a typo'd emit fails loudly in tests instead of minting a new type, and
``tests/events/test_action_sites_pinned.py`` pins that every type here
has a real emitting site.
"""

from __future__ import annotations

from primer.events.registry import known_event_kinds

ACTION_EVENT_TYPES: frozenset[str] = frozenset({
    "session.invoked",
    "session.steered",
    "session.replied",
    "session.parked",
    "session.resumed",
    "session.ended",
    "collection.document_pushed",
    "collection.document_deleted",
    "approval.requested",
    "approval.decided",
    "trigger.fired",
    "mcp.tool_called",
    "tool.called",
    "turn.started",
    "llm.called",
    "graph.node_entered",
    "graph.node_exited",
    "session.wake",
})

_CRUD_VERBS = ("created", "updated", "deleted")


def is_known_event_type(event_type: str) -> bool:
    """Whether ``event_type`` is in the action catalog or is a CRUD
    type of a registered kind."""
    if event_type in ACTION_EVENT_TYPES:
        return True
    kind, dot, verb = event_type.rpartition(".")
    if not dot or verb not in _CRUD_VERBS:
        return False
    return kind in known_event_kinds()


__all__ = ["ACTION_EVENT_TYPES", "is_known_event_type"]
