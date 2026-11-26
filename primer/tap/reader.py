"""TapReader — the single incremental read engine behind every tap surface.

Given a workspace, a :class:`~primer.tap.selector.TapSelector`, and a
:class:`~primer.tap.cursor.TapCursor`, the reader produces
:class:`~primer.tap.event.TapEvent`s by reading the **durable per-session
message logs** (``<state>/sessions/<sid>/``, every segment of the log), applying the
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

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol
from collections.abc import Callable

from primer.model.workspace_session import (
    SessionMessageKind,
    SessionMessageRecord,
    SessionStatus,
    WorkspaceSession,
)
from primer.model.problem_details import record_without_traceback
from primer.model.storage import OffsetPage
from primer.tap.event import TapEvent, record_to_tap_event
from primer.tap.selector import match_events, new_read_budget, session_predicate_for_storage
from primer.workspace.segments import SegmentGap, SessionLog, WorkspaceSource

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
    """The slice of the workspace IO surface the reader needs: what :class:`~primer.workspace.segments.WorkspaceSource` calls.

    Mirrors the real workspace (see :class:`primer.int.workspace.Workspace`): ``list_files`` and ``file_info`` find the log's segments and their
    sizes, ``read_file`` and ``read_file_from`` read them; a file that is absent raises :class:`primer.model.except_.NotFoundError`.
    ``state_path`` is read via ``getattr`` with a default, so an IO object that does not expose it still works.
    """

    async def list_files(self, path: str = ".") -> list[Any]: ...

    async def file_info(self, path: str) -> Any: ...

    async def read_file(self, path: str) -> bytes: ...

    async def read_file_from(self, path: str, offset: int) -> bytes: ...


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def agent_graph_ids(session: WorkspaceSession) -> tuple[str | None, str | None]:
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


def _session_log(workspace_io: Any, session_id: str) -> SessionLog:
    """The session's log (every segment) through the workspace; offsets into it are logical (the bytes of the single file it would be)."""
    state_path = getattr(workspace_io, "state_path", _DEFAULT_STATE_PATH)
    return SessionLog(WorkspaceSource(workspace_io), f"{state_path}/sessions/{session_id}")


async def _read_log(workspace_io: Any, session_id: str, *, offset: int = 0) -> bytes:
    """The session's log from the logical ``offset``; empty when there is no log yet (a new session that has not flushed) or it cannot be read.

    A missing log is a normal condition, not an error, and any read failure is treated as empty, as the sessions router's replay does.
    """
    try:
        return await _session_log(workspace_io, session_id).read_from(offset)
    except SegmentGap:
        logger.warning("tap: session %s has a gap in its log segments; treating it as empty", session_id)
    except Exception as exc:  # noqa: BLE001 - any read failure -> treat as empty
        logger.debug("tap: reading the log of session %s raised %r; treating as empty", session_id, exc)
    return b""


# A tail read starts from this many bytes and doubles until it covers what the caller wants (the records past its seq, or its N most recent).
_TAIL_START_BYTES = 64 * 1024
# A window of more than this is parsed on a worker thread, so a big catch-up does not stall the event loop; a live tick's few lines stay inline.
_OFF_LOOP_BYTES = 128 * 1024
# The backfill never reads more than this of one session's log (its most recent records only).
BACKFILL_MAX_BYTES = 4 * 1024 * 1024


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


async def _read_log_tail(
    workspace_io: Any, session_id: str, *, covers: Callable[[list[bytes]], bool], max_bytes: int | None = None,
) -> tuple[bytes, int]:
    """``(window, start_offset)``: the end of the session's log from a line boundary, wide enough that ``covers(bytes of the window)`` holds.

    The window starts ``_TAIL_START_BYTES`` before the end and doubles until ``covers`` holds, the log's byte 0 is reached or ``max_bytes`` is
    (``None``: no cap). A window that starts inside the log drops its first, possibly partial, line, so ``start_offset`` (a logical offset, as
    ``read_session_since`` takes) is a line start. When the cap is hit and the window holds no complete line, the log's tail is one record
    bigger than the cap: the window ends at the line boundary before it (up to two cap-sized walks back; a tail record longer than twice the
    cap is left alone), so the records before it still count (review 2026-11-23, M-1). Empty log, missing log or an unreadable one:
    ``(b"", 0)``.
    """
    try:
        log = _session_log(workspace_io, session_id)
        segments = await log.segments()
        total = sum(s.size for s in segments)
        size = _TAIL_START_BYTES
        while True:
            start = max(0, total - size)
            window = await log.read_from(start, segments=segments)
            if start > 0:
                cut = window.find(b"\n") + 1  # find == -1 (no newline at all): cut is 0 and the whole window is one partial line
                window, start = window[cut:], start + cut
            if start == 0 or covers(window) or (max_bytes is not None and size >= max_bytes):
                if max_bytes is not None and size >= max_bytes and start > 0 and window.find(b"\n") == -1:
                    # The cap is hit and the window holds no complete line: the tail of the log is one record bigger than the cap. Walk back
                    # from the window's start, cap-sized chunks (two at most), to the line boundary before that record, and end the window
                    # there so the records before it still count (M-1).
                    pos = start
                    boundary: int | None = None
                    for _ in range(2):
                        chunk_start = max(0, pos - max_bytes)
                        chunk = (await log.read_from(chunk_start, segments=segments))[: pos - chunk_start]
                        if pos == total and chunk.endswith(b"\n"):
                            chunk = chunk[:-1]  # that newline ends the giant record itself; it is not a boundary before it
                        p = chunk.rfind(b"\n")
                        if p != -1:
                            boundary = chunk_start + p + 1
                            break
                        pos = chunk_start
                        if pos == 0:
                            break
                    if boundary is not None:
                        new_start = max(0, boundary - max_bytes)
                        window = (await log.read_from(new_start, segments=segments))[: boundary - new_start]
                        if new_start > 0:
                            cut = window.find(b"\n") + 1  # guaranteed: the newline at boundary - 1 is inside
                            window, new_start = window[cut:], new_start + cut
                        return window, new_start
                return window, start
            size *= 2
    except SegmentGap:
        logger.warning("tap: session %s has a gap in its log segments; treating it as empty", session_id)
    except Exception as exc:  # noqa: BLE001 - any read failure -> treat as empty
        logger.debug("tap: reading the tail of the log of session %s raised %r; treating as empty", session_id, exc)
    return b"", 0


def _first_record_at_or_before(raw: bytes, *, after_seq: int, since: datetime | None) -> bool:
    """Whether the window's first record is no newer than the resume point (its seq <= ``after_seq``, or its ts <= ``since``): every
    record past the resume point is then inside the window, as seqs and times only grow along the log. Only the bytes up to the first
    newline are parsed (review 2026-11-23, M-2): the window's first line is its first record, and a later line can only be newer, so the
    rest of the window is never split or parsed for the ``covers`` check. A first line that is not a record decides on its own (``False``:
    the caller widens the window, which is always safe)."""
    nl = raw.find(b"\n")
    record = _parse_record(raw if nl == -1 else raw[: nl + 1])
    if record is None:
        return False
    return record.seq <= after_seq or (since is not None and _aware(record.created_at) <= since)


def _parse_lines(lines: list[bytes], after_seq: int) -> list[SessionMessageRecord]:
    """The parseable records of ``lines`` with ``seq > after_seq`` (pure CPU: runs on a worker thread for a big window)."""
    out: list[SessionMessageRecord] = []
    for line in lines:
        record = _parse_record(line)
        if record is not None and record.seq > after_seq:
            out.append(record)
    return out


_IN_FLIGHT: dict[tuple, "asyncio.Future[Any]"] = {}


async def _single_flight(key: tuple, fetch: Any) -> Any:
    """Run ``fetch()`` once for every concurrent caller with the same ``key``; each gets the same result. Only calls that overlap in time share."""
    pending = _IN_FLIGHT.get(key)
    if pending is not None:
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            if not pending.cancelled():
                raise  # this caller was cancelled, not the read it was waiting on
            return await fetch()  # the caller that was reading went away: read for ourselves
    future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    _IN_FLIGHT[key] = future
    try:
        result = await fetch()
    except asyncio.CancelledError:
        future.cancel()
        raise
    except BaseException as exc:
        future.set_exception(exc)
        future.exception()  # retrieved here, so a read nobody else waited on does not log "never retrieved"
        raise
    else:
        future.set_result(result)
        return result
    finally:
        _IN_FLIGHT.pop(key, None)


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
        record = SessionMessageRecord.model_validate(
            record_without_traceback(json.loads(text)),
        )
    except (json.JSONDecodeError, ValueError):
        return None
    if record.kind == SessionMessageKind.COMPACTION_MARKER:
        return None
    return record


async def read_record_by_seq(
    workspace_io: _WorkspaceReadIO, *, session_id: str, seq: int,
) -> SessionMessageRecord | None:
    """Read the single record at ``seq`` from one session's log (walking its segments newest first).

    Phase 3 stage 7a (01a0518b): the read side of a ``ToolCallTask``'s
    ``record_seq`` pointer (see its own docstring for the ordering
    invariant this backs). Returns ``None`` when the log is missing or
    no record at ``seq`` exists — callers that need a hard guarantee
    the record exists (e.g. the ordering invariant above) raise on a
    ``None`` themselves; this function stays a plain lookup, no opinion
    on what a miss means to its caller.

    No byte-offset index exists for a specific ``seq`` today, so this
    reads the WHOLE log and scans for the match — the same cost
    :func:`read_batch` already pays per session. Callers doing this
    inside a hot per-task loop should be aware a very long session's
    log gets re-read for each lookup; batching multiple tasks' lookups
    against a single log read (if that pattern emerges) is a
    reasonable future optimisation, not built here.
    """
    log = _session_log(workspace_io, session_id)
    try:
        segments = await log.segments()
        for info in reversed(segments):
            lines, _ = _complete_lines(await log.read_segment(info))
            first: int | None = None
            for line in lines:
                record = _parse_record(line)
                if record is None:
                    continue
                if record.seq == seq:
                    return record
                if first is None:
                    first = record.seq
            if first is not None and first <= seq:
                return None  # seqs only grow along the log: nothing older can hold it
    except SegmentGap:
        logger.warning("tap: session %s has a gap in its log segments; no record at seq %s", session_id, seq)
    except Exception as exc:  # noqa: BLE001 - an unreadable log has no record
        logger.debug("tap: reading the log of session %s for seq %s raised %r", session_id, seq, exc)
    return None


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
    since: datetime | None = None,
    tail_limit: int | None = None,
) -> tuple[list[TapEvent], int]:
    """Incrementally read one session's log from logical byte ``from_offset``.

    A FIRST read (``from_offset == 0``) of a session that has a resume point (``after_seq > 0``, or ``since``, a time the caller discards older
    records before) does not read the log from byte 0: it reads a window from the end of the log, doubling it until the window's first record is
    at or before the resume point (every later record is then inside it), so a long session costs the size of what is new, not of its history.
    ``tail_limit`` is the backfill's form of the same: the window covers the ``tail_limit`` most recent records (at most ``BACKFILL_MAX_BYTES`` of
    the log) and only those are returned. The events and the returned offset are the same as a read from byte 0 would give (a ``tail_limit``
    read returns only the newest ``tail_limit`` records). When the newest record alone exceeds the cap, the window ends before it: the backfill
    returns the newest ``tail_limit`` of the records before it (the oversized one is skipped and a live read from the returned offset picks it
    up once whole). A window of more than 128 KB is parsed on a worker thread.

    Reads the session's log (all its segments) and parses only COMPLETE lines starting at
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
    # Only the bytes the log gained since the last tick (the logical offset is into the whole log, whatever its segments). Subscribers of one workspace
    # at the same offset ask for the same bytes in the same tick: one read serves them (single flight; nothing is kept once it lands, so the next tick
    # reads afresh).
    if from_offset == 0 and tail_limit is not None:
        window, from_offset = await _single_flight(
            (id(workspace_io), session.id, "backfill", tail_limit),
            lambda: _read_log_tail(
                # complete lines of the window = its newline count (M-2: covers works on the raw bytes)
                workspace_io, session.id, covers=lambda raw: raw.count(b"\n") > tail_limit + 1, max_bytes=BACKFILL_MAX_BYTES,
            ),
        )
    elif from_offset == 0 and (after_seq > 0 or since is not None):
        window, from_offset = await _single_flight(
            (id(workspace_io), session.id, "tail", after_seq, since),
            lambda: _read_log_tail(
                workspace_io, session.id, covers=lambda raw: _first_record_at_or_before(raw, after_seq=after_seq, since=since),
            ),
        )
    else:
        window = await _single_flight(
            (id(workspace_io), session.id, from_offset), lambda: _read_log(workspace_io, session.id, offset=from_offset),
        )
    if not window:
        return [], from_offset

    lines, consumed = _complete_lines(window)
    next_offset = from_offset + consumed

    agent_id, graph_id = agent_graph_ids(session)
    if len(window) > _OFF_LOOP_BYTES:
        records = await asyncio.to_thread(_parse_lines, lines, after_seq)
    else:
        records = _parse_lines(lines, after_seq)
    if tail_limit is not None:
        records = records[-tail_limit:]
    candidates: list[TapEvent] = [
        record_to_tap_event(
            record,
            workspace_id=workspace_id,
            session_id=session.id,
            agent_id=agent_id,
            graph_id=graph_id,
            cursor=f"{session.id}:{record.seq}",
        )
        for record in records
    ]
    # One batch: a selector with a LIKE is matched off the event loop under
    # one time budget (G6-4); TapSelectorTimeout propagates to the caller.
    keep = await match_events(selector, candidates)
    events = [ev for ev, ok in zip(candidates, keep) if ok]

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
    :func:`session_predicate_for_storage` and pages through ``find`` for a
    complete session set, returned newest-first (ties by id) so the drain
    reads the most recently active sessions first (review 2026-11-23, I2,
    M-4).
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
    # Newest-first (review 2026-11-23, I2, M-4): the drain's ``limit`` fills
    # from the most recently active sessions, and ``read_batch`` stops
    # reading logs the moment the limit is filled -- sessions older than
    # the filled events cannot contribute to this drain's output, so their
    # logs are never read (their cursors stay untouched; the next drain
    # picks them up). The recency key is the one the SSE tap orders by
    # (``last_turn_at``, falling back to ``created_at``; primer/api/routers/
    # tap.py's ``_recency``); ties keep the id order, so the visit order
    # stays deterministic across calls.
    sessions.sort(key=lambda s: s.id or "")
    sessions.sort(
        key=lambda s: _aware(s.last_turn_at or s.created_at), reverse=True
    )
    return sessions


async def read_batch(
    storage_or_provider: Any,
    workspace_io: _WorkspaceReadIO,
    *,
    workspace_id: str,
    selector: "TapSelector",
    cursor: "TapCursor",
    limit: int,
    drained_at: datetime | None = None,
) -> tuple[list[TapEvent], "TapCursor"]:
    """Drain up to ``limit`` events across all in-scope sessions since *cursor*.

    Resolves in-scope sessions via :func:`session_predicate_for_storage` in
    newest-first order, then for each session reads the session log **from
    the cursor's byte-offset hint** (``cursor.resume_offset(session.id)``)
    rather than byte 0, so repeated drains skip already-consumed bytes.

    Correctness guarantee: the byte offset is a *performance hint only* — it is
    valid because the log (the concatenation of its segments) only ever grows: turn writes and the
    compaction marker are byte-appends, so already-consumed bytes never shift.
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

    Visit order is newest-first (sessions by ``last_turn_at``, falling back
    to ``created_at``, ties by id), records by ascending ``seq``: the drain
    reads logs only until ``limit`` events are filled, so sessions older
    than the fill are skipped entirely (their cursors are untouched; the
    next drain picks them up, review 2026-11-23, I2, M-4).

    The token's watermark (review 2026-11-23, I2-3): when ``drained_at`` (the
    caller's clock) is given and the drain is WORKSPACE-WIDE (no ``sessions``
    predicate) and read EVERY in-scope session to the end of its log (the
    ``limit`` cut no session), ``cursor.drained_as_of`` moves to it (max-wins)
    and the ENDED sessions the drain fully drained leave the token
    (``seqs``/``offsets``) -- the MCP token stays bounded as sessions end. A
    scoped drain's completeness never moves the watermark: its scope is a
    subset of the workspace, and a later, broader drain on the same token must
    not skip the sessions the scoped one never read (review 2026-11-23, I2
    r2).  The watermark also gates a skip: a session ABSENT from the cursor
    that is ENDED with ``ended_at`` at or before ``drained_as_of`` is not even
    read -- ENDED is terminal and the drain that set the watermark read its log
    to the end.  A reopen (``ENDED -> CREATED``, which clears ``ended_at`` and
    the status) lifts the skip, and the session is read again from the start.

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
    read_budget = new_read_budget()
    fully_drained: set[str] = set()  # the sessions this drain reached the end of (read, or skipped under the watermark)
    for session in sessions:
        if len(events) >= limit:
            break
        # ENDED, absent from the cursor and ended at or before the watermark: the drain that set it
        # read this log to the end, and ENDED is terminal (a reopen clears ``ended_at`` and the
        # status, so it is read again). Skipping it keeps the token's drop cheap.
        if (
            session.id not in cursor.seqs
            and session.status == SessionStatus.ENDED
            and session.ended_at is not None
            and _aware(session.ended_at) <= _aware(cursor.drained_as_of)
        ):
            fully_drained.add(session.id)
            continue
        resume_seq = cursor.resume_seq(session.id)
        from_offset = cursor.resume_offset(session.id)

        # Seek to the hint offset (logical, into the whole log) - skip bytes we know are already consumed. An offset past the end (a concurrent
        # truncation, which should never happen on an append-only log) reads as empty.
        window = await _read_log(workspace_io, session.id, offset=from_offset)
        if not window:
            fully_drained.add(session.id)
            continue

        lines, _ = _complete_lines(window)

        agent_id, graph_id = agent_graph_ids(session)

        # Parse, map and match in chunks of at most the events still wanted,
        # so a long backlog costs no more than the limit asks for and events
        # past the limit are never mapped or matched (round 2, M3). Every
        # chunk of the read shares ONE time budget: a selector with a LIKE is
        # matched off the event loop (G6-4), and TapSelectorTimeout
        # propagates without the cursor being returned.
        line_offset = from_offset  # absolute offset at the START of the next line
        last_consumed_offset = from_offset  # updated after each consumed record
        index = 0
        while index < len(lines) and len(events) < limit:
            wanted = limit - len(events)
            chunk: list[tuple[int, Any, TapEvent | None]] = []
            candidates = 0
            while index < len(lines) and candidates < wanted:
                line = lines[index]
                index += 1
                line_end = line_offset + len(line) + 1  # +1 for the '\n'
                line_offset = line_end
                record = _parse_record(line)
                ev = None
                if record is not None and record.seq > resume_seq:
                    ev = record_to_tap_event(
                        record,
                        workspace_id=workspace_id,
                        session_id=session.id,
                        agent_id=agent_id,
                        graph_id=graph_id,
                        cursor=f"{session.id}:{record.seq}",
                    )
                    candidates += 1
                chunk.append((line_end, record, ev))
            keep = iter(await match_events(
                selector, [ev for _, _, ev in chunk if ev is not None], budget=read_budget,
            ))
            for line_end, record, ev in chunk:
                if ev is None:
                    # Seq-filter backstop: update offset even for skipped records so
                    # the hint stays as far forward as possible.
                    last_consumed_offset = line_end
                    continue
                # Advance the seq cursor for every consumed record so a filtered-out
                # record is never re-offered on the next drain.
                cursor.advance(session.id, record.seq)
                last_consumed_offset = line_end
                if next(keep):
                    events.append(ev)

        # Record the offset of the last consumed line (not the end of the whole
        # window) so a limit-cut session resumes exactly where we stopped.
        cursor.advance_offset(session.id, last_consumed_offset)
        if index >= len(lines):
            fully_drained.add(session.id)

    # A complete drain (no session cut by the limit) drops the ENDED sessions it drained and, when the scope is
    # the whole workspace, moves the watermark, keeping the token bounded (I2-3). A scoped drain never moves it:
    # its scope is a subset of the workspace, and a later, broader drain on the same token must not skip the
    # sessions the scoped one never read (review 2026-11-23, I2 r2). An incomplete one leaves both alone.
    if drained_at is not None and len(fully_drained) == len(sessions):
        if selector.sessions is None:
            now = _aware(drained_at)
            if now > _aware(cursor.drained_as_of):
                cursor.drained_as_of = now
        cursor.prune_ended({s.id for s in sessions if s.status == SessionStatus.ENDED})

    return events, cursor


__all__ = ["read_session_since", "read_batch", "read_record_by_seq"]
