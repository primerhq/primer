"""Applying a binding switch that was requested while a turn was running.

A running turn owns its binding, so a switch cannot take effect the
moment it is asked for. It is queued on the row and applied at the next
drain checkpoint, before any queued steer is realized, so a follow-up
that was waiting behind the turn runs under the INCOMING binding.

Every caller funnels through :func:`apply_binding_switch`, so the epoch
bump, the re-snapshot and the attribution marker are written in exactly
one place and cannot drift apart.

The switch is one protocol for every caller, under ``session_lifecycle_lock``
(held by the CALLER: this module never takes the non-reentrant lock): RESERVE
the marker's seq with a guarded ``patch_if``, append the marker with the
reserved seq, then ONE fenced ``patch_if`` of the binding fields. A rejected
reservation writes nothing; the old whole-row write from a stale read repeated
a seq a steer had just taken and erased the steer's ``last_seq`` and armed turn.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from pydantic_core import to_jsonable_python

from primer.model.workspace_session import (
    AgentSessionBinding,
    GraphSessionBinding,
    SessionMessageKind,
    SessionMessageRecord,
    WorkspaceSession,
)
from primer.session.persistence import WorkspaceMessageWriter
from primer.session.seq_reservation import reserve_seq

logger = logging.getLogger(__name__)


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


async def _marker_records(workspace_io: Any, row: WorkspaceSession) -> list[dict[str, Any]] | None:
    """The AGENT_MARKER records of the session's ``messages.jsonl``, read whole through the io shim.

    ``None`` when the io has no reader (a test fake), so detection is skipped there. A read that FAILS raises: applying
    a switch without knowing whether an earlier attempt left a marker could reuse its epoch, so the switch stays queued
    instead. There is no tail API, and the whole file is read by other writers already (C5(i)); a switch is rare.
    """
    reader = getattr(workspace_io, "read_state_file", None)
    if reader is None:
        return None
    raw = await reader(row.workspace_id, f"sessions/{row.id}/messages.jsonl")
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    markers: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("kind") == SessionMessageKind.AGENT_MARKER.value:
            markers.append(record)
    return markers


def _marker_epoch(record: dict[str, Any]) -> int:
    try:
        return int((record.get("payload") or {}).get("binding_epoch") or 0)
    except (TypeError, ValueError):
        return 0


async def apply_binding_switch(
    *,
    sessions: Any,
    workspace_io: Any,
    row: WorkspaceSession,
    request: dict[str, Any] | None,
    actor: str,
    resolve_snapshot: Any,
    guard: Mapping[str, Sequence[Any]],
) -> WorkspaceSession | None:
    """Apply a queued switch: re-snapshot, reserve the seq, mark, then ONE fenced write.

    ``resolve_snapshot`` is injected so this module never imports the
    agent or graph storage, and so a deleted target degrades to a
    snapshot-less binding the executor builder resolves live rather than
    failing the switch.

    ``guard`` is the CALLER's precondition on the row (for example the idle
    route: ``{"turn_status": ["idle"], "parked_status": [None]}``; a turn's
    checkpoint: ``{"parked_status": [None]}``; the pool ending a session whose
    park columns are still set: ``{"status": ["ended"]}``), checked atomically
    with the reservation (together with ``last_seq == row.last_seq``, the
    generation the caller read) AND again with the closing write (together with
    ``last_seq == the reserved seq``). It must not name ``last_seq``. Callers
    hold ``session_lifecycle_lock``; this function never takes it.

    Returns the row as written, or ``None`` when the switch was NOT applied:
    the reservation was rejected (the row changed since it was read; NOTHING
    was written) or the closing write was (a park committed after the
    reservation; in one process the lock rules that out, so this is the
    multi-process residual, and the marker stays in the log unapplied).
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

    # An earlier attempt can have left its marker behind (a timeout or a cancellation between the marker and the
    # closing write; the local workspace appends through ``asyncio.to_thread``, which keeps writing after its await is
    # abandoned): a marker ABOVE the row's applied epoch was never applied. Only one AT THE TIP, for this very target and
    # the next epoch, is completed from; every other one is minted past. Completing from one below the tip would write
    # ``next_unprocessed_seq = seq + 1`` and move the drain cursor BACKWARDS.
    markers = await _marker_records(workspace_io, row)
    new_epoch = row.binding_epoch + 1
    seq: int | None = None
    if markers:
        orphans = [m for m in markers if _marker_epoch(m) > row.binding_epoch]
        tip = next((m for m in orphans if m.get("seq") == row.last_seq), None)
        if (
            tip is not None
            and _marker_epoch(tip) == new_epoch
            and (tip.get("payload") or {}).get("to_binding") == _binding_summary(new_binding)
        ):
            seq = row.last_seq  # complete from the orphan: its seq is already reserved and written
        else:
            new_epoch = max([row.binding_epoch, *(_marker_epoch(m) for m in markers)]) + 1

    if seq is None:
        # (1) RESERVE the marker's seq: the guard and "last_seq is still what the caller read" are one atomic condition,
        # so a steer that took last_seq + 1 in the meantime rejects this and nothing has been written.
        seq = await reserve_seq(sessions, row.id, last_seq=row.last_seq, where=guard)
        if seq is None:
            logger.info(
                "binding switch of session %s not applied: the row changed since it was read; nothing was written",
                row.id,
            )
            return None

        # (2) append the marker AT the reserved seq and flush.
        writer = WorkspaceMessageWriter(
            workspace_io=workspace_io, session_id=row.id, start_seq=seq - 1,
        )
        appended = await writer.append(SessionMessageRecord(
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
        if appended != seq:  # pragma: no cover - the writer was seeded from the reservation
            raise RuntimeError(f"binding switch of session {row.id}: marker took seq {appended}, reserved {seq}")
        await writer.flush()

    # (3) ONE fenced write of the binding fields. The marker is a closed structural record, neither a user input nor
    # a terminal, so the pairing count is untouched and the drain cursor may pass it; leaving it behind would hand the
    # next route_steer slow path a record it cannot classify.
    updated = await sessions.patch_if(
        row.id,
        to_jsonable_python({
            "binding": new_binding,
            "binding_epoch": new_epoch,
            "pending_binding_switch": None,
            "next_unprocessed_seq": seq + 1,
        }),
        where={**guard, "last_seq": [seq]},
    )
    if updated is None:
        logger.warning(
            "binding switch of session %s: the row changed after the marker at seq %d was reserved and written; the "
            "switch was not applied and its marker stays in the log", row.id, seq,
        )
    return updated


__all__ = [
    "agent_marker_payload",
    "apply_binding_switch",
    "build_switched_binding",
]
