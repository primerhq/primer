"""On-demand compaction for sessions: the guards and the marker write.

The LLM call itself is injected rather than imported, so this module
stays unit-testable without a provider and the router keeps the
FastAPI-shaped work (resolving the agent, the profile and the client).

Compaction is append-only like everything else in the log: the marker
carries the summary and the span it replaces, and the read-time walk
folds the rows before it. Nothing is deleted, so the event history
survives for audit while the prompt shrinks.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from primer.model.except_ import ConflictError
from primer.model.workspace_session import (
    SessionMessageKind,
    SessionMessageRecord,
    WorkspaceSession,
)
from primer.session.persistence import WorkspaceMessageWriter


@dataclass(frozen=True)
class CompactionOutcome:
    """What the caller needs to answer the request and update the row."""

    compaction_marker_seq: int
    summary: str
    tokens_before: int
    tokens_after: int


def guard_compactable(row: WorkspaceSession) -> None:
    """Raise unless this session can be compacted right now.

    Idle-only, and strictly wider than the chat guard was: chat had no
    parks, but a parked session is mid-turn and its resume still needs
    the history the fold would replace.

    A graph binding is rejected outright. Graph internals see graph
    state rather than session history, so there is no conversation for
    a graph-bound session to compact.
    """
    if getattr(row.binding, "kind", None) == "graph":
        raise ConflictError(
            f"session {row.id!r} is graph-bound; compaction applies to "
            "agent bindings"
        )
    if row.turn_status != "idle" or row.parked_status is not None:
        raise ConflictError(
            f"session {row.id!r} is not idle; compaction requires no turn "
            "in flight"
        )


async def compact_session(
    *,
    row: WorkspaceSession,
    workspace_io: Any,
    history: list,
    run_compaction: Any,
) -> CompactionOutcome:
    """Summarise ``history`` and append the marker that folds it.

    ``run_compaction`` is an async callable taking the history and
    returning an object with summary_text, tokens_before, tokens_after
    and optionally model_name. Injecting it keeps the provider wiring in
    the router and makes this path testable in milliseconds.
    """
    result = await run_compaction(history)

    # Seeded from the row's last_seq at write time: the summarising call
    # takes seconds, so the caller re-reads the row first and a
    # concurrent write may have moved the cursor.
    replaced_to = row.last_seq
    writer = WorkspaceMessageWriter(
        workspace_io=workspace_io,
        session_id=row.id,
        start_seq=replaced_to,
    )
    seq = await writer.append(SessionMessageRecord(
        seq=1,  # overwritten by the writer's monotonic counter
        kind=SessionMessageKind.COMPACTION_MARKER,
        payload={
            "summary": result.summary_text,
            # A prior fold left the cursor past the rows it replaced, so
            # this compaction starts where that one stopped.
            "replaced_from_seq": row.next_unprocessed_seq or 1,
            "replaced_to_seq": replaced_to,
            "model": getattr(result, "model_name", None),
            "tokens_before": result.tokens_before,
            "tokens_after": result.tokens_after,
            "created_at": datetime.now(UTC).isoformat(),
        },
        created_at=datetime.now(UTC),
    ))
    await writer.flush()
    return CompactionOutcome(
        compaction_marker_seq=seq,
        summary=result.summary_text,
        tokens_before=result.tokens_before,
        tokens_after=result.tokens_after,
    )


__all__ = ["CompactionOutcome", "compact_session", "guard_compactable"]
