"""Applying a binding switch that was requested while a turn was running.

A running turn owns its binding, so a switch cannot take effect the
moment it is asked for. It is queued on the row and applied at the next
drain checkpoint, before any queued steer is realized, so a follow-up
that was waiting behind the turn runs under the INCOMING binding.

Every caller funnels through :func:`apply_binding_switch`, so the epoch
bump, the re-snapshot and the attribution marker are written in exactly
one place and cannot drift apart.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from primer.model.workspace_session import (
    AgentSessionBinding,
    GraphSessionBinding,
    SessionMessageKind,
    SessionMessageRecord,
    WorkspaceSession,
)
from primer.session.persistence import WorkspaceMessageWriter


def build_switched_binding(
    row: WorkspaceSession,
    request: dict[str, Any],
    *,
    agent_snapshot: Any | None = None,
    graph_snapshot: Any | None = None,
):
    """Build the binding a switch request asks for.

    Handles all four transitions: agent to agent, agent to graph, graph
    to agent, and a profile-only change that keeps the target.
    """
    kind = request.get("kind")
    profile_id = request.get("profile_id")
    if kind == "graph":
        return GraphSessionBinding(
            graph_id=request["graph_id"],
            profile_id=profile_id,
            graph_snapshot=graph_snapshot,
        )
    return AgentSessionBinding(
        agent_id=request["agent_id"],
        profile_id=profile_id,
        agent_snapshot=agent_snapshot,
    )


def agent_marker_payload(
    *,
    from_binding: dict[str, Any],
    to_binding: dict[str, Any],
    actor: str,
    binding_epoch: int,
) -> dict[str, Any]:
    """Attribution for a hand-off, as the transcript records it.

    Carries the epoch because the record and its tap event have to be
    informationally identical: a client that missed the event and reads
    the log later must be able to reconstruct the same binding history.
    """
    return {
        "from_binding": from_binding,
        "to_binding": to_binding,
        "actor": actor,
        "binding_epoch": binding_epoch,
        "created_at": datetime.now(UTC).isoformat(),
    }


def _binding_summary(binding: Any) -> dict[str, Any]:
    kind = getattr(binding, "kind", None)
    out: dict[str, Any] = {"kind": kind}
    if kind == "graph":
        out["graph_id"] = getattr(binding, "graph_id", None)
    else:
        out["agent_id"] = getattr(binding, "agent_id", None)
    out["profile_id"] = getattr(binding, "profile_id", None)
    return out


async def apply_binding_switch(
    *,
    sessions: Any,
    workspace_io: Any,
    row: WorkspaceSession,
    request: dict[str, Any] | None,
    actor: str,
    resolve_snapshot: Any,
) -> WorkspaceSession:
    """Apply a queued switch: re-snapshot, bump the epoch, mark, persist.

    ``resolve_snapshot`` is injected so this module never imports the
    agent or graph storage, and so a deleted target degrades to a
    snapshot-less binding the executor builder resolves live rather than
    failing the switch.
    """
    if not request:
        return row

    provisional = build_switched_binding(row, request)
    # Re-snapshot the INCOMING target: a switch means the session should
    # run that agent or graph as it is defined now, not as it was when
    # some earlier binding was frozen.
    snapshot = await resolve_snapshot(provisional)
    if getattr(provisional, "kind", None) == "graph":
        new_binding = build_switched_binding(
            row, request, graph_snapshot=snapshot,
        )
    else:
        new_binding = build_switched_binding(
            row, request, agent_snapshot=snapshot,
        )

    new_epoch = row.binding_epoch + 1
    writer = WorkspaceMessageWriter(
        workspace_io=workspace_io, session_id=row.id, start_seq=row.last_seq,
    )
    seq = await writer.append(SessionMessageRecord(
        seq=1,  # overwritten by the writer's monotonic counter
        kind=SessionMessageKind.AGENT_MARKER,
        payload=agent_marker_payload(
            from_binding=_binding_summary(row.binding),
            to_binding=_binding_summary(new_binding),
            actor=actor,
            binding_epoch=new_epoch,
        ),
        created_at=datetime.now(UTC),
    ))
    await writer.flush()

    updated = row.model_copy(update={
        "binding": new_binding,
        "binding_epoch": new_epoch,
        "pending_binding_switch": None,
        "last_seq": seq,
        # The marker is a closed structural record, neither a user input
        # nor a terminal, so the pairing count is untouched and the
        # cursor may pass it. Leaving it behind would hand the next
        # route_steer slow path a record it cannot classify.
        "next_unprocessed_seq": seq + 1,
    })
    await sessions.update(updated)
    return updated


__all__ = [
    "agent_marker_payload",
    "apply_binding_switch",
    "build_switched_binding",
]
