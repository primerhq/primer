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
"""

from __future__ import annotations

import logging
from typing import Any

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
            "resume_metadata": dict,
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


def resolve_pending_gate(
    blob: dict[str, Any],
    *,
    tool_call_id: str,
    kind: str | None = None,
) -> dict[str, Any] | None:
    """The one pending entry matching ``tool_call_id`` (and ``kind`` if given).

    ``kind`` narrows to one tool_name (e.g. ``"_approval"``) when a caller
    knows only entries of that kind can answer the request. Mirrors
    :func:`primer.api.routers.yields._graph_ask_user_dispatch`'s collision
    handling: two concurrent fan-out siblings can share a raw provider
    tool_call_id, which the REST wire contract has no field to
    disambiguate, so a collision resolves to the first match and logs a
    warning rather than raising.
    """
    matches = [
        entry for entry in enumerate_pending_gates(blob)
        if entry.get("tool_call_id") == tool_call_id
        and (kind is None or entry.get("kind") == kind)
    ]
    if len(matches) > 1:
        logger.warning(
            "resolve_pending_gate: %d pending entries share "
            "tool_call_id=%r (kind=%r); resolving the first",
            len(matches), tool_call_id, kind,
        )
    return matches[0] if matches else None


__all__ = ["enumerate_pending_gates", "resolve_pending_gate"]
