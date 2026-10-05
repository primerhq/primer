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

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from primer.model.except_ import ConflictError, ValidationError
from primer.model.workspace_session import (
    SessionMessageKind,
    SessionMessageRecord,
    WorkspaceSession,
)
from primer.session.persistence import WorkspaceMessageWriter


_NOTHING_TO_COMPACT = {
    "empty_head": "there is no earlier history that can be summarised without dropping input the model has not answered",
    "protected_over_budget": "the input the model has not answered already fills the context window",
    "fixed_over_budget": "the system prompt and tool schemas alone fill the context window",
}


class NothingToCompact(ValidationError):
    """A manual compaction could not summarise anything (422). ``reason`` is the strategy's verdict
    (``CompactedTurn.unreducible``) and travels in the problem details' ``extensions``."""

    def __init__(self, reason: str | None) -> None:
        why = _NOTHING_TO_COMPACT.get(reason or "", "there is nothing it can summarise")
        super().__init__(f"nothing to compact ({reason or 'unknown'}): {why}")
        self.reason = reason

    @property
    def problem_extensions(self) -> dict[str, str | None]:
        return {"reason": self.reason}


logger = logging.getLogger(__name__)


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
    reload_history: Any = None,
) -> CompactionOutcome:
    """Summarise ``history`` and append the marker that folds it.

    ``run_compaction`` is an async callable taking the history and
    returning an object with summary_text, tokens_before, tokens_after
    and optionally model_name. Injecting it keeps the provider wiring in
    the router and makes this path testable in milliseconds.

    The summarising call takes seconds, and a steer can land in that time. The marker folds
    every line before it, so ``reload_history`` (an async callable returning the history as
    the reader sees it NOW) is read again just before the write and the Message lines that
    are not in ``history`` are appended to the marker's kept tail. (A line written in the
    milliseconds between that read and the append is not covered: this path has no handle on
    the session's messages lock.)
    """
    result = await run_compaction(history)
    if not result.summary_text:
        # Nothing could be summarised (the history is the input the model has not
        # answered, or too short to have a head): a marker with no summary would
        # fold the whole history into nothing.
        raise NothingToCompact(getattr(result, "unreducible", None))

    # Seeded from the row's last_seq at write time: the summarising call
    # takes seconds, so the caller re-reads the row first and a
    # concurrent write may have moved the cursor.
    kept_tail = list(getattr(result, "kept_tail", None) or [])
    summary_after = int(getattr(result, "summary_after", 0) or 0)
    if reload_history is not None:
        current = await reload_history()
        if len(current) > len(history):
            if [m.role for m in current[: len(history)]] == [m.role for m in history]:
                kept_tail += list(current[len(history):])
            else:
                logger.warning(
                    "session %s: the history changed under a manual compaction (another marker or a rewind); "
                    "not carrying the lines written since its snapshot",
                    row.id,
                )
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
            # The verdict, as the executor's marker records it.
            "outcome": getattr(result, "outcome", "summarised"),
            "unreducible": getattr(result, "unreducible", None),
            "trigger_tokens": getattr(result, "trigger_tokens", None),
            # ``tokens_before`` / ``tokens_after`` count this part of the prompt too, as far as the
            # caller could measure it, as the executor's marker records.
            "fixed_overhead_tokens": getattr(result, "fixed_overhead_tokens", 0),
            "created_at": datetime.now(UTC).isoformat(),
            # The tail kept verbatim after the summary: without it the fold
            # would drop it (see reconstruct_compacted_history).
            **(
                {"kept_tail_messages": [json.loads(m.model_dump_json()) for m in kept_tail]}
                if kept_tail else {}
            ),
            **({"summary_after": summary_after} if summary_after and kept_tail else {}),
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
