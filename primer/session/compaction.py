"""On-demand compaction for sessions: the guards and the marker write.

The LLM call itself is injected rather than imported, so this module
stays unit-testable without a provider and the router keeps the
FastAPI-shaped work (resolving the agent, the profile and the client).

Compaction is append-only like everything else in the log: the marker
carries the summary and the span it replaces, and the read-time walk
folds the rows before it. Nothing is deleted, so the event history
survives for audit while the prompt shrinks.

The marker's seq is RESERVED, not seeded from a read (S2a PR-5b, D-A1-02):
the summarising call takes seconds and a steer can land in that window,
run a turn and park. The caller re-reads the row under the session's
lifecycle lock and reserves ``last_seq + 1`` with a guarded ``patch_if``
(the idle-unparked guard plus ``last_seq == the row just read``,
``primer.session.seq_reservation``); a rejection means the row moved, so
NOTHING is written and the caller answers 409 (a retry re-summarises).
The reservation is the row's ``last_seq`` write, so there is no later
whole-row write: the old whole-row write of the row read before the
summariser repeated the steer's seq and erased its park columns.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
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
from primer.session.seq_reservation import reserve_seq


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
    summary_input_reduced: dict[str, int] | None = None
    """What the compaction did to the summariser's input when its first call overflowed, else ``None``."""


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


#: The fence of the marker's seq reservation: the route's own up-front guard (an idle, unparked
#: session), checked atomically with ``last_seq == the row the caller read`` by ``reserve_seq``.
_IDLE_UNPARKED: Mapping[str, Sequence[Any]] = {"turn_status": ["idle"], "parked_status": [None]}


async def summarise_session_history(*, history: list, run_compaction: Any) -> Any:
    """Phase 1, no lock and no storage: run the injected summariser on ``history``.

    ``run_compaction`` is an async callable taking the history and
    returning an object with summary_text, tokens_before, tokens_after
    and optionally model_name. The router supplies the real one (it
    resolves the agent, the profile and the client); injecting it keeps
    the provider wiring out of this module and the path testable in
    milliseconds. The route runs this phase OUTSIDE the session's
    lifecycle lock: the call takes seconds, and holding the lock that
    long would queue every Cancel, Stop, steer, pause and switch of the
    session behind it.
    """
    result = await run_compaction(history)
    if not result.summary_text:
        # Nothing could be summarised (the history is the input the model has not
        # answered, or too short to have a head): a marker with no summary would
        # fold the whole history into nothing.
        raise NothingToCompact(getattr(result, "unreducible", None))
    return result


async def append_compaction_marker(
    *,
    sessions: Any,
    row: WorkspaceSession,
    workspace_io: Any,
    result: Any,
    history: list,
    reload_history: Any = None,
) -> CompactionOutcome:
    """Phase 2, the CALLER holding the session's lifecycle lock: reload the kept tail, reserve
    the marker's seq and append the marker AT the reserved seq.

    ``result`` is the summariser's outcome (:func:`summarise_session_history`). The summarising
    call takes seconds and a steer can land in that time, so ``reload_history`` (an async callable
    returning the history as the reader sees it NOW) is read again just before the reservation and
    the Message lines that are not in ``history`` are appended to the marker's kept tail. (A line
    written in the milliseconds between that read and the append is not covered: this path has no
    handle on the session's messages lock.)

    The reservation fences on the route's own up-front guard (idle, unparked) plus ``last_seq ==``
    the row the caller read: a steer that ran a turn and parked in the window set the park columns
    and moved ``last_seq``, so the fence rejects it and NOTHING is written (the caller answers 409;
    a retry re-summarises) instead of the marker reusing the steer's seq. The reservation is the
    row's ``last_seq`` write, so no row write follows the append.
    """
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
    # (1) RESERVE the marker's seq: the guard and "last_seq is still what the caller read" are one
    # atomic condition, so a steer that took last_seq + 1 (or parked) in the meantime rejects this
    # and nothing has been written.
    seq = await reserve_seq(sessions, row.id, last_seq=row.last_seq, where=_IDLE_UNPARKED)
    if seq is None:
        raise ConflictError(
            f"session {row.id!r} is no longer idle: it moved while the summariser ran; the marker "
            "was not written, retry the compaction"
        )
    # (2) append the marker AT the reserved seq and flush. Seeded from the reservation, not from a
    # read: the fence held the row at ``seq - 1``, so the first appended record takes ``seq``.
    replaced_to = row.last_seq
    writer = WorkspaceMessageWriter(
        workspace_io=workspace_io,
        session_id=row.id,
        start_seq=seq - 1,
    )
    appended = await writer.append(SessionMessageRecord(
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
            # As the executor's marker: only when the summariser's input had to be reduced.
            **(
                {"summary_input_reduced": reduced}
                if (reduced := getattr(result, "summary_input_reduced", None)) else {}
            ),
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
    if appended != seq:  # pragma: no cover - the writer was seeded from the reservation
        raise RuntimeError(f"compaction marker of session {row.id}: took seq {appended}, reserved {seq}")
    await writer.flush()
    # The reservation is the ``last_seq`` write: the route writes nothing further to the row.
    return CompactionOutcome(
        compaction_marker_seq=seq,
        summary=result.summary_text,
        tokens_before=result.tokens_before,
        tokens_after=result.tokens_after,
        summary_input_reduced=getattr(result, "summary_input_reduced", None) or None,
    )


async def compact_session(
    *,
    row: WorkspaceSession,
    workspace_io: Any,
    history: list,
    run_compaction: Any,
    reload_history: Any = None,
    sessions: Any,
) -> CompactionOutcome:
    """Summarise ``history`` and append the marker that folds it (both phases).

    The route splits the phases instead: the summariser runs outside the session's lifecycle lock
    and :func:`append_compaction_marker` under it. A caller that composes both without the lock
    still gets the fenced reservation (the fence is the guarantee, the lock is the discipline);
    ``sessions`` is the storage of the session row.
    """
    result = await summarise_session_history(history=history, run_compaction=run_compaction)
    return await append_compaction_marker(
        sessions=sessions,
        row=row,
        workspace_io=workspace_io,
        result=result,
        history=history,
        reload_history=reload_history,
    )


__all__ = [
    "CompactionOutcome",
    "append_compaction_marker",
    "compact_session",
    "guard_compactable",
    "summarise_session_history",
]
