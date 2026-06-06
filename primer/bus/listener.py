"""Background listener that flips parked sessions to resumable.

Spec: ``docs/superpowers/specs/2026-06-07-f10c-session-yield-park-resume-design.md``.

For each event on the bus, the listener finds every session parked on
that ``event_key``, flips the row ``parked -> resumable`` (stamping the
resume payload), and re-arms the engine lease so the active claim loop
re-claims and resumes the session.

The flip is guarded so only a row still in ``'parked'`` state is
advanced; a second event for the same key is a no-op (the payload is
not overwritten). One listener per app is sufficient: the bus is
broadcast, but the guarded flip means only the first publisher wins.
"""

from __future__ import annotations

import asyncio
import logging

from primer.int.claim import ClaimEngine, ClaimKind
from primer.int.event_bus import EventBus
from primer.int.storage import Storage
from primer.model.storage import FieldRef, OffsetPage, Op, Predicate, Value


logger = logging.getLogger(__name__)


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
        self._stopping = False

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(
            self._run(), name="yield-event-listener",
        )

    async def stop(self) -> None:
        self._stopping = True
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        self._task = None

    async def _run(self) -> None:
        if self._bus is None:
            return
        sub = self._bus.subscribe()
        try:
            async for event in sub:
                if self._stopping:
                    break
                await self._handle_event(event)
        finally:
            await sub.aclose()

    async def _handle_event(self, event) -> None:
        """Flip every session parked on ``event.event_key`` to resumable and
        re-arm its engine lease.

        Errors are logged but do not break the listener loop; the next
        event is processed regardless.
        """
        try:
            predicate = Predicate(
                left=Predicate(
                    left=FieldRef(name="parked_status"),
                    op=Op.EQ,
                    right=Value(value="parked"),
                ),
                op=Op.AND,
                right=Predicate(
                    left=FieldRef(name="parked_event_key"),
                    op=Op.EQ,
                    right=Value(value=event.event_key),
                ),
            )
            # event_key encodes the session + tool_call (e.g.
            # "ask_user:<session_id>:<tool_call_id>"), so in practice it
            # matches at most one parked row; the 200 cap is a generous
            # safety bound, not a real fan-out limit.
            page = await self._storage.find(predicate, OffsetPage(length=200))
            flipped = 0
            for sess in page.items:
                # Re-read guard: only advance rows still 'parked'.
                if sess.parked_status != "parked":
                    continue
                state = dict(sess.parked_state or {})
                state["resume_event_payload"] = dict(event.payload or {})
                updated = sess.model_copy(update={
                    "parked_status": "resumable",
                    "parked_state": state,
                })
                await self._storage.update(updated)
                # Re-arm the engine lease (park dropped it). mark_resumable
                # upserts a fresh claimable lease when none exists.
                await self._engine.mark_resumable(ClaimKind.SESSION, sess.id)
                flipped += 1
            if flipped:
                logger.info(
                    "yield-event-listener: resumed %d session(s) for "
                    "event_key=%r", flipped, event.event_key,
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "yield-event-listener: handle_event failed for "
                "event_key=%r: %s", event.event_key, exc,
            )


__all__ = ["YieldEventListener"]
