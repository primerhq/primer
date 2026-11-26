"""Background listener that flips parked sessions to resumable.

Spec: ``docs/superpowers/specs/2026-06-07-f10c-session-yield-park-resume-design.md``.

For each event on the bus, the listener finds every session parked on
that ``event_key``, flips the row ``parked -> resumable`` (stamping the
resume payload), and re-arms the engine lease so the active claim loop
re-claims and resumes the session.

Bus noise never reaches the session query: the bus also carries every
token delta, phase tick and hint topic, and those are not park keys. The
filter is a DENY-list of those known noise topics (:data:`_NOISE_KEY_PREFIXES`),
not an allow-list of park keys: a park key minted by a new feature that this
module does not know about costs one query, where an allow-list would lose its
wake silently.

The subscription's mailbox is bounded: when the listener falls behind, its
oldest events are DROPPED. A drop is a loss, so the listener is signalled
(``on_reconnect``, which the buses also use for real reconnects) and logs the
drop count, rate-limited. It does NOT scan the parked rows: that read (graph
checkpoints included) would run under the very overload that caused the drop.
The lost events are recovered from their durable second paths (the event
log's flip sink, ``emit_session_wake``, the timeout sweepers, the wait's own
timeout).

The flip is guarded so only a row still in ``'parked'`` state is
advanced; a second event for the same key is a no-op (the payload is
not overwritten). One listener per app is sufficient: the bus is
broadcast, but the guarded flip means only the first publisher wins.
"""

from __future__ import annotations

import asyncio
import logging
import time

from primer.int.claim import ClaimEngine
from primer.int.event_bus import EventBus
from primer.int.storage import Storage
from primer.session.yields import durably_mark_session_resumable


logger = logging.getLogger(__name__)

#: Bus topics that are never a park key, and carry the bus's volume: the
#: session stream (``session:<id>:delta|tick|cancel|terminal|claimable``), the
#: events-log hint, the harness lifecycle topics, rate-limit release and the
#: cache-invalidation topics. Everything else may be a park key and costs one
#: session query.
_NOISE_KEY_PREFIXES = (
    "session:",
    "events_appended",
    "harness:",
    "harness-claimable",
    "rate_limit_released:",
    "invalidate:",
)

#: The least time between two overflow warnings (the drop counts accumulate
#: between them).
_LOSS_LOG_INTERVAL_SECONDS = 30.0


class YieldEventListener:
    """Background task: subscribe to the event bus, resume parked sessions.

    Lifecycle:

    * ``start()`` - schedules an asyncio task running the listener loop.
    * ``stop()`` - cancels the task and awaits its exit. Idempotent.
    """

    def __init__(
        self,
        *,
        bus: EventBus | None,
        session_storage: Storage,
        engine: ClaimEngine,
    ) -> None:
        self._bus = bus
        self._storage = session_storage
        self._engine = engine
        self._task: asyncio.Task | None = None
        self._sub = None
        self._dropped_logged = 0
        self._last_loss_log = float("-inf")
        self._loss_flush_task: asyncio.Task | None = None
        self._stopping = False

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(
            self._run(), name="yield-event-listener",
        )

    async def stop(self) -> None:
        self._stopping = True
        if self._loss_flush_task is not None:
            self._loss_flush_task.cancel()
            try:
                await self._loss_flush_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._loss_flush_task = None
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _run(self) -> None:
        if self._bus is None:
            return
        # ``on_reconnect`` is the loss signal for BOTH transports: a real
        # LISTEN reconnect (Postgres) and a mailbox overflow (in-memory,
        # which has no droppable transport - a queue that overflowed IS a
        # loss). The hook logs the drop count.
        sub = self._sub = self._bus.subscribe(on_reconnect=self._on_loss)
        try:
            await sub.ready()
            async for event in sub:
                if self._stopping:
                    break
                await self._handle_event(event)
        finally:
            self._sub = None
            await sub.aclose()

    def _on_loss(self) -> None:
        """The bus dropped events this subscription never saw (a reconnect,
        or the mailbox overflowing while the consumer was stalled).

        Logs the count dropped since the last warning, at most once per
        :data:`_LOSS_LOG_INTERVAL_SECONDS`. Drops that land INSIDE the
        interval are logged at the interval's end by a scheduled flush, not
        only when the next loss arrives: a consumer that stays stalled never
        reads again, and its drops must not go unlogged (review 2026-11-23,
        I1). It reads no rows: the wakes the dropped events carried are
        recovered by their durable second paths.
        """
        # The subscription's running ``dropped`` counter (both buses); a
        # transport without one (a real reconnect) reports no count.
        total = getattr(self._sub, "dropped", None)
        now = time.monotonic()
        if now - self._last_loss_log >= _LOSS_LOG_INTERVAL_SECONDS:
            self._log_loss(total)
        elif total is not None and total > self._dropped_logged:
            self._schedule_loss_flush()

    def _log_loss(self, total) -> None:
        """Log the drops accumulated since the last warning and reset the window."""
        dropped = None if total is None else total - self._dropped_logged
        if total is not None:
            self._dropped_logged = total
        self._last_loss_log = time.monotonic()
        logger.warning(
            "yield listener: the bus dropped events (mailbox overflow or "
            "reconnect; %s dropped since the last warning); a lost wake is "
            "recovered by the event log's flip sink, the timeout sweepers "
            "or the wait's own timeout",
            "an unknown number" if dropped is None else dropped,
        )

    def _schedule_loss_flush(self) -> None:
        """A one-shot log at the current interval's end (at most one pending)."""
        if self._loss_flush_task is not None and not self._loss_flush_task.done():
            return
        self._loss_flush_task = asyncio.create_task(
            self._flush_suppressed_loss(), name="yield-listener-loss-flush",
        )

    async def _flush_suppressed_loss(self) -> None:
        """The scheduled end-of-window log for the drops the interval suppressed."""
        wait = _LOSS_LOG_INTERVAL_SECONDS - (time.monotonic() - self._last_loss_log)
        if wait > 0:
            await asyncio.sleep(wait)
        if self._stopping or self._sub is None:
            return
        total = getattr(self._sub, "dropped", None)
        if total is not None and total > self._dropped_logged:
            self._log_loss(total)

    async def _flip_rows(self, rows, event) -> int:
        """Flip the given parked rows to resumable, stamping the payload +
        the specific key that fired, and re-arming the engine lease.

        Delegates the per-row guarded flip to the shared
        :func:`primer.session.yields.durably_mark_session_resumable` so the
        listener and the REST reply handlers (which now perform the same
        durable flip inline - arch review D-C2) can never diverge. Multi-event
        parks (``parked_event_keys`` set) ACCUMULATE replies into
        ``parked_state['resume_event_payloads']`` (keyed by the fired
        tool_call_id) and may be advanced even from ``resumable``; single-event
        parks stamp the singular ``resume_event_payload`` and only advance from
        ``parked``.
        """
        flipped = 0
        for sess in rows:
            did = await durably_mark_session_resumable(
                sess,
                event_key=event.event_key,
                payload=event.payload,
                session_storage=self._storage,
                engine=self._engine,
            )
            if did:
                flipped += 1
        return flipped

    async def _handle_event(self, event) -> None:
        """Flip every session parked on ``event.event_key`` to resumable and
        re-arm its engine lease.

        The cheap pre-query filter: the known noise topics
        (:data:`_NOISE_KEY_PREFIXES`: token deltas, ticks, hint and
        invalidation topics) are never a park key and cost no session query.
        Any other key, known to this module or not, is queried.

        Single-event parks match on the singular ``parked_event_key`` (the
        fast path). Multi-event parks (a graph superstep with several
        human-interaction nodes) may be woken by a reply to a NON-primary
        key, which is a member of ``parked_event_keys`` but not the
        singular ``parked_event_key`` - handled by a bounded membership
        fallback, gated to human-reply and machine-wake keys so the common
        path stays a single keyed query.

        Errors are logged but do not break the listener loop; the next
        event is processed regardless.
        """
        key = getattr(event, "event_key", None)
        if not isinstance(key, str) or key.startswith(_NOISE_KEY_PREFIXES):
            return
        try:
            from primer.session.yields import flip_sessions_parked_on

            flipped = await flip_sessions_parked_on(
                key, event.payload,
                session_storage=self._storage, engine=self._engine,
            )
            if flipped:
                logger.info(
                    "yield-event-listener: resumed %d session(s) for "
                    "event_key=%r", flipped, key,
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "yield-event-listener: handle_event failed for "
                "event_key=%r: %s", key, exc,
            )


__all__ = ["YieldEventListener"]
