"""Ephemeral delta frames + per-part_id micro-batching for the tap.

Non-durable layer (Phase 1, spec
``docs/superpowers/plans/2026-08-28-p1-delta-stream-spec.md`` section 1-3, 9).

Deltas are the *live* content path that the durable log deliberately omits:
``translate_stream_event`` coalesces ``TextDelta`` / ``ReasoningDelta`` /
``ToolCallDelta`` into ONE record per call (they return ``None``), so the
``messages.jsonl`` log has no tokens until the buffer flushes. This module
emits small, id-keyed ``part_start`` / ``*_delta`` / ``part_end`` frames over
the event bus so a client can render a part as it forms, then reconcile the
accumulated live part to the durable record (which carries the same
``part_id``) when it arrives.

Design properties (spec section 3.3, 4.4, 9.2):
* **Ephemeral** - never written to ``messages.jsonl``, never read by
  ``read_session_since`` (the tap re-reads the log only on a durable tick),
  and never advanced by the tap cursor. A reconnect re-derives from the
  durable log, not from deltas.
* **Best-effort** - a bus publish failure is swallowed (debug log); the UI
  silently falls back to final-record behaviour (today's UX). The durable
  record always completes the part, so a lost or late delta is harmless.
* **Id-keyed** - every frame carries ``part_id``; the client accumulates
  deltas per ``part_id`` and replaces the accumulated part when the durable
  record with the same ``part_id`` arrives.

Sync/async split (the load-bearing contract):
``translate_stream_event`` is a *synchronous* function (it is called from the
worker with no ``await``), so it cannot drive an async publish. The buffer
therefore splits into a sync half (``on_delta`` / ``close`` - just mutate
per-part state) and an async half (``flush`` / ``aclose`` - do the actual
publish). The worker starts a periodic flush timer (:meth:`start`) and calls
:meth:`aclose` at turn end; the translator calls the sync half on each event.

The bus channel is ``session:{sid}:delta`` (content-bearing, distinct from the
``session:{sid}:tick`` pointer). A ``PostgresEventBus`` carries it across
pods; an ``InMemoryEventBus`` carries it within a process - the same code
either way (one path, no special-casing).
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

# The EventBus channel the delta frames ride on. Distinct from the durable
# tick pointer (``session:{sid}:tick``) so the tap can fan deltas out
# separately and the cursor path is never touched.
DELTA_EVENT_PREFIX = "session:"
DELTA_EVENT_SUFFIX = ":delta"

# Postgres caps NOTIFY payloads at 8000 bytes (primer/bus/postgres.py raises
# above MAX_NOTIFY_PAYLOAD_BYTES = 7900). A delta NOTIFY body is
# ``{"event_key": "session:{sid}:delta", "payload": {frame JSON}}``; the
# event_key + JSON wrapper + the frame's fixed fields (class/session_id/
# part_id/kind/ts) cost ~230 bytes, so a safe *delta-string* cap that keeps
# the whole body under 7900 is ~7600 bytes (spec section 9.2-A3).
_MAX_DELTA_BYTES = 7600

# Flush cadence for the periodic batch (spec section 9.2-A2). Aligns with the
# WorkspaceMessageWriter 100 ms buffer (primer/session/persistence.py) so a
# delta and its eventual durable record land in the same ~100 ms window.
_FLUSH_INTERVAL_S = 0.100

# The three content kinds a part can carry. Used as plain strings so the
# (synchronous) translate_stream_event can drive the buffer without importing
# this module or an enum.
KIND_TEXT = "text"
KIND_REASONING = "reasoning"
KIND_TOOL = "tool"

# Publish is fire-and-forget; a failure must never error the turn.
DeltaPublish = Callable[[str, dict], Awaitable[None]]


class DeltaClass(StrEnum):
    """Wire-level class for an ephemeral :class:`DeltaFrame`.

    These are a separate namespace from the durable :class:`TapEventClass`
    (primer/tap/event.py) so an old client that parses the tap stream and
    keys off ``class`` ignores them harmlessly (it has no case for them, and
    the frame carries no ``seq`` so the existing seq guard drops it).
    """

    PART_START = "part_start"
    TEXT_DELTA = "text_delta"
    REASONING_DELTA = "reasoning_delta"
    TOOL_INPUT_DELTA = "tool_input_delta"
    PART_END = "part_end"
    # agent_phase transition (01a04d91-a7a0). Not id-keyed like the four
    # above - a phase belongs to the turn, not a part - so it rides its
    # own PhaseFrame rather than DeltaFrame (part_id has no meaning here).
    PHASE = "phase"


# kind -> the delta class for a *_delta frame.
_KIND_TO_CLASS: dict[str, DeltaClass] = {
    KIND_TEXT: DeltaClass.TEXT_DELTA,
    KIND_REASONING: DeltaClass.REASONING_DELTA,
    KIND_TOOL: DeltaClass.TOOL_INPUT_DELTA,
}


class DeltaFrame(BaseModel):
    """One ephemeral, id-keyed delta frame ready for the tap SSE.

    ``class`` is a reserved keyword in Python, so the field is ``class_`` but
    serialises as ``"class"`` on the wire (mirrors TapEvent). The frame has
    **no ``seq``**: it is ephemeral, so it never advances the tap cursor and
    is never replayed from the log.
    """

    model_config = ConfigDict(populate_by_name=True)

    class_: DeltaClass = Field(..., alias="class")
    session_id: str
    part_id: str
    # The content kind of the part (text / reasoning / tool); present on every
    # frame so part_start / part_end (whose class is kind-agnostic) still tell
    # the client which part they belong to.
    kind: str | None = None
    # Non-empty for *_delta frames; None for part_start / part_end.
    delta: str | None = None


class PhaseFrame(BaseModel):
    """One ephemeral agent_phase transition frame (01a04d91-a7a0).

    Rides the same ``session:{sid}:delta`` channel as :class:`DeltaFrame`
    (an old client keying off ``class`` ignores an unrecognised value
    harmlessly, same as the delta classes) but is turn-scoped rather than
    part-scoped, and published immediately on each transition rather than
    batched - a phase change is a single small event, not a stream of
    tokens, so DeltaBuffer's accumulate-then-flush machinery would only
    add latency here for no benefit. ``turn_no`` fences a client against
    a frame that arrives after a NEWER turn already started (a delayed
    publish from a turn that has since ended) - compare against the
    session row's own ``turn_no`` / ``agent_phase_turn_no`` and ignore a
    mismatch, mirroring the row field's own fencing.
    """

    model_config = ConfigDict(populate_by_name=True)

    class_: Literal[DeltaClass.PHASE] = Field(default=DeltaClass.PHASE, alias="class")
    session_id: str
    phase: Literal["thinking", "responding", "executing", "waiting"]
    turn_no: int


async def publish_phase_frame(
    publish: DeltaPublish, *, session_id: str, phase: str, turn_no: int,
) -> None:
    """Publish one :class:`PhaseFrame`. Best-effort, mirrors DeltaBuffer's
    own publish-failure handling: a degraded bus must not error the turn,
    and the row write + turns.jsonl audit entry are the durable/recoverable
    sources of truth this frame is purely a low-latency shortcut for."""
    frame = PhaseFrame(session_id=session_id, phase=phase, turn_no=turn_no)
    try:
        await publish(
            f"{DELTA_EVENT_PREFIX}{session_id}{DELTA_EVENT_SUFFIX}",
            frame.model_dump(by_alias=True, mode="json"),
        )
    except Exception:  # noqa: BLE001 - bus is best-effort; degrade silently
        logger.debug("phase frame: publish failed", exc_info=True)


def part_id(node_id: str | None, kind: str) -> str:
    """Stable part_id shared by the delta frames and the durable record.

    Node-scoped so parallel fan-out nodes never collide (the coalesce state
    already keys text/reasoning by node_id, primer/session/persistence.py).
    ``None`` -> ``"x"`` so an agent-only part still has a stable id.
    """
    return f"{node_id or 'x'}:{kind}"


def _split_by_bytes(text: str, max_bytes: int) -> list[str]:
    """Split *text* into chunks each <= ``max_bytes`` UTF-8 bytes.

    Splits only on character boundaries (never mid-character), so a CJK
    delta is never cut. Returns ``[text]`` when it already fits.
    """
    if len(text.encode("utf-8")) <= max_bytes:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    current_bytes = 0
    for ch in text:
        ch_bytes = len(ch.encode("utf-8"))
        if current_bytes + ch_bytes > max_bytes and current:
            chunks.append("".join(current))
            current = []
            current_bytes = 0
        current.append(ch)
        current_bytes += ch_bytes
    if current:
        chunks.append("".join(current))
    return chunks


class _Part:
    """Per-part_id accumulation state inside a :class:`DeltaBuffer`."""

    __slots__ = ("kind", "pending", "started", "closed")

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.pending: list[str] = []
        self.started = False  # part_start emitted
        self.closed = False   # part_end emitted


class DeltaBuffer:
    """Accumulates delta text per part_id, batches it, publishes to the bus.

    One buffer per turn (created by the worker, see
    :mod:`primer.session.dispatch`). The translator
    (:func:`primer.session.persistence.translate_stream_event`) calls the
    *synchronous* :meth:`on_delta` per stream delta and the *synchronous*
    :meth:`close` when the durable record for a part is produced. The actual
    bus publish happens on the async :meth:`flush` (the periodic timer) and
    :meth:`aclose`, so the translator never blocks on I/O.

    :param session_id: the session the frames belong to.
    :param publish: the EventBus publish (``event_key, payload``); failures
        are swallowed (a degraded bus must not error the turn).
    :param now: clock, injectable for tests.
    """

    def __init__(
        self,
        *,
        session_id: str,
        publish: DeltaPublish,
        now: Callable[[], datetime] | None = None,
        interval_s: float = _FLUSH_INTERVAL_S,
    ) -> None:
        self._session_id = session_id
        self._publish = publish
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._interval_s = interval_s
        self._parts: OrderedDict[str, _Part] = OrderedDict()
        self._timer: asyncio.Task | None = None
        self._closed = False

    # -- lifecycle ------------------------------------------------------
    async def start(self) -> None:
        """Start the periodic flush task. Idempotent."""
        if self._timer is not None:
            return
        self._timer = asyncio.create_task(
            self._run(), name=f"delta-buffer-{self._session_id}"
        )

    async def _run(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._interval_s)
                if self._closed:
                    return
                await self.flush()
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - a batch hiccup must not kill the turn
            logger.debug("delta buffer: periodic flush failed", exc_info=True)

    async def aclose(self) -> None:
        """Final flush + close every part; cancel the timer. Idempotent.

        Flushes any remaining pending deltas (part_start + the deltas) and
        emits ``part_end`` for parts the translator closed, so a part that
        never saw a periodic flush still finishes. A part left open (no
        close called) gets its deltas but no part_end - harmless, the durable
        record already completed it.
        """
        if self._closed:
            return
        self._closed = True
        if self._timer is not None:
            self._timer.cancel()
            try:
                await self._timer
            except asyncio.CancelledError:
                pass
            self._timer = None
        for pid, p in self._parts.items():
            await self._flush_part(pid, p)
            if p.closed:
                p.pending = []
                await self._emit(DeltaClass.PART_END, pid, None, kind=p.kind)

    # -- sync half (called from translate_stream_event) ---------------
    def on_delta(self, pid: str, kind: str, delta: str) -> None:
        """Record a delta for a part (batched, not published immediately).

        A no-op once the part is closed (the durable record has landed) or the
        buffer is closed, so a late delta cannot append to a finalized part.
        """
        if self._closed or not delta:
            return
        p = self._parts.get(pid)
        if p is None:
            p = _Part(kind)
            self._parts[pid] = p
        if p.closed:
            return
        p.pending.append(delta)

    def close(self, pid: str) -> None:
        """Mark a part closed (sync). The ``part_end`` frame is emitted later
        by the async :meth:`aclose`; the durable record is the source of
        truth, so a delayed part_end is harmless.
        """
        p = self._parts.get(pid)
        if p is None or p.closed:
            return
        p.closed = True

    # -- async half (timer + aclose) -----------------------------------
    async def flush(self) -> None:
        """Emit every part's accumulated delta (no ``part_end``)."""
        for pid, p in self._parts.items():
            await self._flush_part(pid, p)

    async def _flush_part(self, pid: str, p: _Part) -> None:
        if not p.pending:
            return
        if not p.started:
            await self._emit(DeltaClass.PART_START, pid, None, kind=p.kind)
            p.started = True
        text = "".join(p.pending)
        p.pending = []
        if not text:
            return
        cls = _KIND_TO_CLASS.get(p.kind, DeltaClass.TEXT_DELTA)
        for chunk in _split_by_bytes(text, _MAX_DELTA_BYTES):
            await self._emit(cls, pid, chunk, kind=p.kind)

    async def _emit(
        self, cls: DeltaClass, pid: str, delta: str | None, kind: str | None,
    ) -> None:
        frame = DeltaFrame(
            class_=cls,
            session_id=self._session_id,
            part_id=pid,
            kind=kind,
            delta=delta,
        )
        try:
            await self._publish(
                f"{DELTA_EVENT_PREFIX}{self._session_id}{DELTA_EVENT_SUFFIX}",
                frame.model_dump(by_alias=True, mode="json"),
            )
        except Exception:  # noqa: BLE001 - bus is best-effort; degrade silently
            logger.debug("delta buffer: publish failed", exc_info=True)


__all__ = [
    "DELTA_EVENT_PREFIX",
    "DELTA_EVENT_SUFFIX",
    "DeltaBuffer",
    "DeltaClass",
    "DeltaFrame",
    "DeltaPublish",
    "KIND_REASONING",
    "KIND_TEXT",
    "KIND_TOOL",
    "PhaseFrame",
    "part_id",
    "publish_phase_frame",
]
