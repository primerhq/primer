"""Enumerate + resolve pending human-decision gates on a parked session.

A parked ``WorkspaceSession`` carries exactly one pending item in the
common (non-graph) case: ``parked_state['yielded']``. A graph park is
different -- a single superstep can suspend on SEVERAL nodes at once (a
fan-out with concurrent approval gates, or a mix of tool-call approvals
and agent-node ``ask_user`` yields), and only the FIRST of those is
projected onto the top-level ``yielded`` blob
(:meth:`primer.graph._checkpoint._CheckpointMixin._build_pending_park_yield`).
The rest live only in ``parked_state['graph_checkpoint']``'s
``pending_toolcalls`` / ``pending_agent_yields`` lists.

Before this module, every REST consumer read ``yielded`` alone, so a
reply aimed at any gate but the primary one had nothing to match against
and 404'd -- even though :mod:`primer.channel.inbox` already resolves any
entry correctly for channel replies. :func:`enumerate_pending_gates` and
:func:`resolve_pending_gate` are the ONE shared implementation for both
directions (list everything pending / find one by tool_call_id), used by
``workspaces.py``'s session yields lister and both
``tool_approval.py`` pending/respond routes, so the three call sites
can't drift the way the primary-only projection and the inbox's own
matcher already have.

Deliberately reads the checkpoint's raw ``pending_toolcalls`` /
``pending_agent_yields`` -- NOT
:func:`primer.worker.yield_runtime.merge_pending_dispatch`'s
``pending_dispatch``-based view. That view is purpose-built for channel
prompts and denormalises tool-call approval entries down to
``{"original_call": ...}``, dropping ``policy_id`` / ``approval_type`` /
``gate_reason`` / ``approvers`` and the entry's own ``parked_event_key``
entirely (channel dispatch only ever needs a human-readable prompt +
the ability to reconstruct the unscoped key by convention). REST needs
the real stored event_key (a graph fan-out gate's key may be
node-scoped) plus the full metadata for approver enforcement and the
audit record, so this module goes straight to the source fields instead.

Known, accepted gap: a checkpoint written before 575c9b1d can carry a
``pending_toolcalls``/``pending_agent_yields`` entry whose own
``resume_metadata`` lacks ``original_call`` (main's top-level
``yielded``-projection read had its own reconstruction for that legacy
shape, which this module does not reproduce) -- verified real but not
worth a fallback here: that pre-575c9b1d window was brief and no park
from it survived into this deployment.
"""

from __future__ import annotations

import logging
from typing import Any

from primer.model.yield_ import gate_id_of
from primer.session.gate_token import gate_token_matches
from primer.session.yields import _tool_call_id_for


logger = logging.getLogger(__name__)


def enumerate_pending_gates(blob: dict[str, Any]) -> list[dict[str, Any]]:
    """Every pending human-decision entry on a parked_state blob.

    Each entry is normalised to::

        {
            "kind": str,               # tool_name: "_approval", "ask_user", ...
            "node_id": str | None,     # graph node/instance id; None for a
                                        # non-graph (agent-session/chat) park
            "tool_call_id": str | None,
            "event_key": str | None,
            "resume_metadata": dict,     # carries the gate's ``gate_id`` when it has one
        }

    A non-graph park yields at most one entry, built from
    ``blob['yielded']``. A graph park yields one entry per pending
    tool-call approval (``graph_checkpoint['pending_toolcalls']``) followed
    by one per pending agent yield (``graph_checkpoint['pending_agent_yields']``)
    -- toolcalls first, matching
    :meth:`_CheckpointMixin._build_pending_park_yield`'s own primary-pick
    order, so callers that only look at ``[0]`` see the same "primary"
    entry that endpoint already projects today.

    Returns ``[]`` for an unparked/empty blob.
    """
    checkpoint = blob.get("graph_checkpoint")
    if checkpoint:
        entries: list[dict[str, Any]] = []
        for p in checkpoint.get("pending_toolcalls") or []:
            entries.append({
                "kind": p.get("tool_name") or "_approval",
                "node_id": p.get("node_id"),
                "tool_call_id": p.get("tool_call_id"),
                "event_key": p.get("parked_event_key"),
                "resume_metadata": dict(p.get("resume_metadata") or {}),
            })
        for p in checkpoint.get("pending_agent_yields") or []:
            entries.append({
                "kind": p.get("tool_name") or "",
                "node_id": p.get("node_id"),
                "tool_call_id": p.get("tool_call_id"),
                "event_key": p.get("event_key"),
                "resume_metadata": dict(p.get("resume_metadata") or {}),
            })
        return entries

    yielded: dict[str, Any] = blob.get("yielded") or {}
    tool_name = yielded.get("tool_name")
    if not tool_name:
        return []
    return [{
        "kind": tool_name,
        "node_id": None,
        "tool_call_id": _tool_call_id_for(blob),
        "event_key": yielded.get("event_key"),
        "resume_metadata": dict(yielded.get("resume_metadata") or {}),
    }]


_KEY_FIELD_BY_LIST = {"pending_toolcalls": "parked_event_key", "pending_agent_yields": "event_key"}
"""The field of each checkpoint list that holds the event key an entry waits on."""


def fired_key_names_a_pending_entry(checkpoint: dict[str, Any], event_key: str | None) -> bool:
    """Whether ``event_key`` is the key one of the checkpoint's pending human entries waits on."""
    if not event_key:
        return False
    return any(
        entry.get(field) == event_key
        for list_name, field in _KEY_FIELD_BY_LIST.items()
        for entry in checkpoint.get(list_name) or []
    )


def pending_entries(
    checkpoint: dict[str, Any], list_name: str, *, tool_call_id: str | None, event_key: str | None = None,
) -> list[dict[str, Any]]:
    """The entries of ``checkpoint[list_name]`` a reply answers (C-033 round 2, ticket 01a11fc6-0cce).

    The provider repeats ``tool_call_id`` across rounds AND across fan-out siblings of one superstep, so the raw id alone can name several entries. The event
    key a reply fired is the entry's own, so when it names a pending entry the selection is by it, in BOTH lists (a key that belongs to an agent-node yield
    must not also select a tool_call entry that shares the raw id). A key that names no entry (a park written before keys were node-scoped), or none at
    all (the key-less legacy drain), selects by the raw id as before.
    """
    entries = checkpoint.get(list_name) or []
    if fired_key_names_a_pending_entry(checkpoint, event_key):
        field = _KEY_FIELD_BY_LIST[list_name]
        return [entry for entry in entries if entry.get(field) == event_key]
    return [entry for entry in entries if entry.get("tool_call_id") == tool_call_id]


def resolve_pending_gate(
    blob: dict[str, Any],
    *,
    tool_call_id: str,
    kind: str | None = None,
    gate_id: str | None = None,
    event_key: str | None = None,
) -> dict[str, Any] | None:
    """The one pending entry matching ``tool_call_id`` (and ``kind`` / ``gate_id`` if given).

    ``kind`` narrows to one tool_name (e.g. ``"_approval"``) when a caller
    knows only entries of that kind can answer the request. Mirrors
    :func:`primer.api.routers.yields._graph_ask_user_dispatch`'s collision
    handling: two concurrent fan-out siblings can share a raw provider
    tool_call_id, which only ``gate_id`` can disambiguate: a caller that
    names the gate it answers gets exactly that entry (``None`` when the
    id given belongs to no pending entry, e.g. a gate since replaced by
    one under the same tool_call_id; a token shorter than a full id, which
    a platform with a tight limit sends, matches by prefix), and a caller
    that names none gets
    the first match with a warning rather than an exception.
    """
    entries = enumerate_pending_gates(blob)
    # The event key a reply fired names its gate exactly (see :func:`pending_entries`); the raw id decides only when the key names none.
    by_key = bool(event_key) and any(entry.get("event_key") == event_key for entry in entries)
    matches = [
        entry for entry in entries
        if (entry.get("event_key") == event_key if by_key else entry.get("tool_call_id") == tool_call_id)
        and (kind is None or entry.get("kind") == kind)
        and (gate_id is None or gate_token_matches(gate_id_of(entry.get("resume_metadata")), gate_id))
    ]
    if len(matches) > 1:
        logger.warning(
            "resolve_pending_gate: %d pending entries share "
            "tool_call_id=%r (kind=%r); resolving the first",
            len(matches), tool_call_id, kind,
        )
    return matches[0] if matches else None


__all__ = ["enumerate_pending_gates", "fired_key_names_a_pending_entry", "pending_entries", "resolve_pending_gate"]
