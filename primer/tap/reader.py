"""TapReader — the single incremental read engine behind every tap surface.

Given a workspace, a :class:`~primer.tap.selector.TapSelector`, and a
:class:`~primer.tap.cursor.TapCursor`, the reader produces
:class:`~primer.tap.event.TapEvent`s by reading the **durable per-session
message logs** (``<state>/sessions/<sid>/messages.jsonl``), applying the
selector, and advancing the cursor.  It is **stateless** — all position lives
in the cursor (drain path) or the caller-held byte offset (live path), so it is
reconnect-safe and horizontally scalable (no per-tap server buffer).

Two read paths, per the design (§2.2) and plan Task 1.4:

* :func:`read_session_since` — the single-session **incremental** fast-path for
  the live SSE tick loop.  It reads from a caller-held byte ``from_offset``,
  parses only COMPLETE newline-terminated lines, tolerates the message
  writer's buffered partial flush (16 KB / 100 ms — see
  :mod:`primer.session.persistence`) by leaving an incomplete trailing line
  unconsumed, and returns the byte offset after the last complete line so the
  caller never re-scans (§8 "incremental read correctness").

* :func:`read_batch` — the multi-session **drain** for the MCP surface.  It
  resolves in-scope sessions via
  :func:`~primer.tap.selector.session_predicate_for_storage`, reads each from
  the start filtering by ``seq > cursor.resume_seq(sid)``, maps + filters,
  accumulates up to ``limit`` total, and advances the cursor per session.

Both paths reuse the real storage layer, the real workspace ``read_file`` IO,
and :func:`~primer.tap.event.record_to_tap_event` — no parallel models.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Protocol

from primer.model.workspace_session import (
    SessionMessageKind,
    SessionMessageRecord,
    WorkspaceSession,
)
from primer.model.storage import OffsetPage
from primer.tap.event import TapEvent, record_to_tap_event
from primer.tap.selector import event_matches, session_predicate_for_storage

if TYPE_CHECKING:
    from primer.tap.cursor import TapCursor
    from primer.tap.selector import TapSelector

logger = logging.getLogger(__name__)

# Default state-repo root inside a workspace; matches the convention used by
# the sessions router (``getattr(workspace, "state_path", ".state")``).
_DEFAULT_STATE_PATH = ".state"

# How many session rows to pull per storage page when resolving the in-scope
# set for a drain.  The drain is bounded by ``limit`` events, not sessions, so
# a generous page keeps the common case to a single round-trip.
_SESSION_PAGE_LEN = 200


class _WorkspaceReadIO(Protocol):
    """The slice of the workspace IO surface the reader needs.

    Mirrors the real workspace ``read_file`` (see
    :meth:`primer.int.workspace.Workspace.read_file`) — a single coroutine
    returning the file bytes and raising
    :class:`primer.model.except_.NotFoundError` when the file is absent.
    ``state_path`` is read via ``getattr`` with a default, so an IO object that
    does not expose it still works.
    """

    async def read_file(self, path: str) -> bytes: ...


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _agent_graph_ids(session: WorkspaceSession) -> tuple[str | None, str | None]:
    """Derive ``(agent_id, graph_id)`` from a session's binding.

    ``session.binding`` is a discriminated union — an
    :class:`~primer.model.workspace_session.AgentSessionBinding` carries
    ``agent_id`` (and no ``graph_id``); a
    :class:`~primer.model.workspace_session.GraphSessionBinding` carries
    ``graph_id`` (and no ``agent_id``).  We read both via ``getattr`` (the
    same pattern as ``primer.worker.pool`` / ``graph_resume_coordinator``) so
    exactly one is populated and the other is ``None``.
    """
    binding = session.binding
    agent_id = getattr(binding, "agent_id", None)
    graph_id = getattr(binding, "graph_id", None)
    return agent_id, graph_id


def _messages_path(workspace_io: Any, session_id: str) -> str:
    """Build the ``messages.jsonl`` path for *session_id* inside the workspace."""
    state_path = getattr(workspace_io, "state_path", _DEFAULT_STATE_PATH)
    return f"{state_path}/sessions/{session_id}/messages.jsonl"


async def _read_raw(
    workspace_io: _WorkspaceReadIO, path: str, session_id: str,
) -> bytes | None:
    """Read *path*; return ``None`` when the file is missing (or unreadable).

    A missing ``messages.jsonl`` (new session that has not flushed yet) is a
    normal condition, not an error — mirrors the sessions router's replay
    behaviour.
    """
    from primer.model.except_ import NotFoundError

    try:
        return await workspace_io.read_file(path)
    except NotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001 — any read failure → treat as empty
        logger.debug(
            "tap: read_file(%s) for session %s raised %r; treating as empty",
            path, session_id, exc,
        )
        return None


def _complete_lines(raw: bytes) -> tuple[list[bytes], int]:
    """Split *raw* into COMPLETE newline-terminated lines + consumed-byte count.

    Only lines terminated by ``\\n`` are returned; if *raw* ends mid-line (the
    message writer's buffered partial flush), the trailing fragment is left
    unconsumed.  The returned int is the number of bytes covered by the
    complete lines — i.e. the offset just past the last ``\\n``.
    """
    last_nl = raw.rfind(b"\n")
    if last_nl == -1:
        return [], 0
    consumed = last_nl + 1
    complete = raw[:consumed]
    # splitlines() drops the trailing empty element a final "\n" would create.
    return complete.splitlines(), consumed


def _parse_record(line: bytes) -> SessionMessageRecord | None:
    """Parse one jsonl line into a record; return ``None`` if unparseable.

    COMPACTION_MARKER records are treated as unparseable (``None``) here so
    they are skipped by both read paths: the marker is an internal
    history-management record, not activity, and it shares a seq with the
    turn's first streamed record (the executor assigns it max_event_seq+1 out
    of band from the dispatch writer). Skipping it WITHOUT advancing the cursor
    means that shared seq can never cause the real record to be dropped.
    """
    text = line.strip()
    if not text:
        return None
    try:
        record = SessionMessageRecord.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValueError):
        return None
    if record.kind == SessionMessageKind.COMPACTION_MARKER:
        return None
    return record


# ---------------------------------------------------------------------------
# read_session_since — incremental single-session path (live SSE)
# ---------------------------------------------------------------------------


async def read_session_since(
    workspace_io: _WorkspaceReadIO,
    *,
    workspace_id: str,
    session: WorkspaceSession,
    after_seq: int,
    selector: "TapSelector",
    from_offset: int = 0,
) -> tuple[list[TapEvent], int]:
    """Incrementally read one session's log from byte ``from_offset``.

    Reads ``messages.jsonl`` and parses only COMPLETE lines starting at
    ``from_offset``.  A trailing partial line (buffered partial flush) is NOT
    consumed; the returned offset points before it, so the next tick re-reads
    that fragment once it is whole.

    For each complete record with ``seq > after_seq``, the record is mapped via
    :func:`record_to_tap_event` (injecting ``workspace_id``, ``session.id``, and
    the session's ``agent_id``/``graph_id``) and filtered via
    :func:`event_matches`.

    The per-event ``cursor`` string is set to a stable placeholder
    (``"<session_id>:<seq>"``).  This live path serves the SSE loop, which
    OVERWRITES the cursor on each frame with the encoded
    :class:`~primer.tap.cursor.TapCursor` token (the seq-vector across all
    in-scope sessions) before transmission — a single session's offset cannot
    encode the multi-session cursor on its own.  See the SSE task.

    Returns:
        ``(events, next_offset)`` where ``next_offset`` is the byte offset
        after the last COMPLETE line consumed (pass it back next tick).  A
        missing log file yields ``([], from_offset)``.
    """
    path = _messages_path(workspace_io, session.id)
    raw = await _read_raw(workspace_io, path, session.id)
    if raw is None:
        return [], from_offset

    # Slice off everything before the caller's offset so we never re-scan.
    if from_offset >= len(raw):
        return [], from_offset
    window = raw[from_offset:]

    lines, consumed = _complete_lines(window)
    next_offset = from_offset + consumed

    agent_id, graph_id = _agent_graph_ids(session)
    events: list[TapEvent] = []
    for line in lines:
        record = _parse_record(line)
        if record is None or record.seq <= after_seq:
            continue
        ev = record_to_tap_event(
            record,
            workspace_id=workspace_id,
            session_id=session.id,
            agent_id=agent_id,
            graph_id=graph_id,
            cursor=f"{session.id}:{record.seq}",
        )
        if event_matches(selector, ev):
            events.append(ev)

    return events, next_offset


# ---------------------------------------------------------------------------
# read_batch — multi-session drain (MCP)
# ---------------------------------------------------------------------------


async def _resolve_sessions(
    storage_or_provider: Any,
    *,
    workspace_id: str,
    selector: "TapSelector",
) -> list[WorkspaceSession]:
    """Resolve the in-scope :class:`WorkspaceSession` rows for a drain.

    Accepts either a storage *provider* (exposing
    ``get_storage(WorkspaceSession)``) or a session *store* directly (exposing
    ``find``).  Builds the workspace-scoped predicate via
    :func:`session_predicate_for_storage` and pages through ``find`` ordered by
    ``id`` for a stable session order.
    """
    store = storage_or_provider
    get_storage = getattr(storage_or_provider, "get_storage", None)
    if get_storage is not None:
        store = get_storage(WorkspaceSession)

    predicate = session_predicate_for_storage(workspace_id, selector)

    sessions: list[WorkspaceSession] = []
    offset = 0
    while True:
        page = OffsetPage(offset=offset, length=_SESSION_PAGE_LEN)
        resp = await store.find(predicate, page)
        sessions.extend(resp.items)
        if len(resp.items) < _SESSION_PAGE_LEN:
            break
        offset += _SESSION_PAGE_LEN
    # Stable order by id so the drain is deterministic across calls.
    sessions.sort(key=lambda s: s.id or "")
    return sessions


async def read_batch(
    storage_or_provider: Any,
    workspace_io: _WorkspaceReadIO,
    *,
    workspace_id: str,
    selector: "TapSelector",
    cursor: "TapCursor",
    limit: int,
) -> tuple[list[TapEvent], "TapCursor"]:
    """Drain up to ``limit`` events across all in-scope sessions since *cursor*.

    Resolves in-scope sessions via :func:`session_predicate_for_storage`, then
    for each session (stable order by id) reads ``messages.jsonl`` **from the
    cursor's byte-offset hint** (``cursor.resume_offset(session.id)``) rather
    than byte 0, so repeated drains skip already-consumed bytes.

    Correctness guarantee: the byte offset is a *performance hint only* — it is
    valid because ``messages.jsonl`` is append-only (no rewrite or compaction).
    The ``seq > cursor.resume_seq(session.id)`` filter remains the authoritative
    backstop: a stale or wrong offset causes at most a harmless re-read of some
    records from an earlier position, never a skip or duplicate.

    For each complete record with ``seq > cursor.resume_seq(session.id)``:
    * the record is mapped via :func:`record_to_tap_event` and filtered via
      :func:`event_matches`;
    * ``cursor.advance(session.id, seq)`` is called so filtered-out records are
      never re-offered on the next drain;
    * ``cursor.advance_offset(session.id, offset)`` is updated to point just
      past the LAST CONSUMED complete line.  When ``limit`` cuts a session
      mid-file the offset reflects only the lines actually consumed, so the next
      drain resumes from exactly the right position (no skip, no dup).

    Order is stable: sessions sorted by id, records by ascending ``seq``.

    The same per-event ``cursor`` placeholder convention as
    :func:`read_session_since` applies (``"<session_id>:<seq>"``); the MCP
    surface returns the encoded :class:`TapCursor` token alongside the batch as
    ``next_cursor``.

    Returns:
        ``(events, cursor)`` — the (mutated, same-instance) cursor reflects
        every consumed record.  A missing log file contributes no events.
    """
    sessions = await _resolve_sessions(
        storage_or_provider, workspace_id=workspace_id, selector=selector,
    )

    events: list[TapEvent] = []
    for session in sessions:
        if len(events) >= limit:
            break
        resume_seq = cursor.resume_seq(session.id)
        from_offset = cursor.resume_offset(session.id)

        path = _messages_path(workspace_io, session.id)
        raw = await _read_raw(workspace_io, path, session.id)
        if raw is None:
            continue

        # Seek to the hint offset — skip bytes we know are already consumed.
        # If the offset is somehow past EOF (e.g. a concurrent truncation, which
        # should never happen on an append-only log) treat it as empty.
        if from_offset >= len(raw):
            continue
        window = raw[from_offset:]

        lines, _ = _complete_lines(window)

        agent_id, graph_id = _agent_graph_ids(session)

        # Walk lines, tracking the running absolute byte offset after each line
        # so we can record the offset of the last CONSUMED line precisely.
        line_offset = from_offset  # absolute offset at the START of current line
        last_consumed_offset = from_offset  # updated after each consumed record

        for line in lines:
            line_end = line_offset + len(line) + 1  # +1 for the '\n'
            if len(events) >= limit:
                break
            record = _parse_record(line)
            line_offset = line_end  # advance regardless — line is processed
            if record is None or record.seq <= resume_seq:
                # Seq-filter backstop: update offset even for skipped records so
                # the hint stays as far forward as possible.
                last_consumed_offset = line_end
                continue
            ev = record_to_tap_event(
                record,
                workspace_id=workspace_id,
                session_id=session.id,
                agent_id=agent_id,
                graph_id=graph_id,
                cursor=f"{session.id}:{record.seq}",
            )
            # Advance the seq cursor for every consumed record so a filtered-out
            # record is never re-offered on the next drain.
            cursor.advance(session.id, record.seq)
            last_consumed_offset = line_end
            if event_matches(selector, ev):
                events.append(ev)

        # Record the offset of the last consumed line (not the end of the whole
        # window) so a limit-cut session resumes exactly where we stopped.
        cursor.advance_offset(session.id, last_consumed_offset)

    return events, cursor


__all__ = ["read_session_since", "read_batch"]
