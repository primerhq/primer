"""EventDispatcher: delivers the durable event log to its subscribers.

One leader-elected loop per install (the TimerScheduler idiom): every
pod runs the supervisor, the LeaderElector picks the worker. The loop
wakes on the ``events_appended`` bus hint and falls back to polling,
reads each unpaused :class:`EventSubscription`'s events above its
cursor in batches, applies the three-tier filter, invokes the sink per
match in id order, and advances the cursor.

Delivery is at-least-once. A sink failure holds the cursor (the next
tick retries); after ``max_failures`` consecutive failures on the same
event the dispatcher skips it with an error log - ``converge`` is
idempotent, so a skip self-heals on the entity's next event.

A subscription seen without any stored cursor starts at the CURRENT
max id: subscriptions consume from creation onward, they do not replay
history (the bootstrap regeneration covers pre-existing state for the
CDC case).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from primer.bus.scheduler_tasks import _BackgroundTask
from primer.events.filters import matches
from primer.events.recorder import EVENTS_APPENDED_KEY
from primer.int.coordinator import ROLE_EVENT_DISPATCHER, ROLE_EVENT_RETENTION
from primer.model.event import (
    Event,
    EventSubscription,
    LogSink,
    SessionWakeSink,
)
from primer.model.storage import OffsetPage

logger = logging.getLogger(__name__)


class EventDispatcher(_BackgroundTask):
    role = ROLE_EVENT_DISPATCHER

    def __init__(
        self,
        *,
        storage_provider: Any,
        event_bus: Any = None,
        provider_registry: Any = None,
        semantic_search_registry: Any = None,
        poll_seconds: float = 5.0,
        batch: int = 200,
        max_failures: int = 5,
    ) -> None:
        super().__init__(name="event-dispatcher")
        self._sp = storage_provider
        self._bus = event_bus
        self._provider_registry = provider_registry
        self._semantic_search_registry = semantic_search_registry
        self._poll = poll_seconds
        self._batch = batch
        self._max_failures = max_failures
        self._wake = asyncio.Event()
        # (subscription_id, event_id) -> consecutive sink failures.
        self._fail_counts: dict[tuple[str, int], int] = {}

    # -- loop -------------------------------------------------------------

    async def _run(self) -> None:
        hint_task: asyncio.Task | None = None
        if self._bus is not None:
            hint_task = asyncio.create_task(
                self._hint_loop(), name="event-dispatcher-hints",
            )
        try:
            while not self._stopping:
                try:
                    await self.drain_once()
                except Exception:  # noqa: BLE001 - the loop must survive
                    logger.exception("event-dispatcher: drain failed")
                try:
                    await asyncio.wait_for(
                        self._wake.wait(), timeout=self._poll,
                    )
                except TimeoutError:
                    pass
                except asyncio.CancelledError:
                    break
                self._wake.clear()
        finally:
            if hint_task is not None:
                hint_task.cancel()
                try:
                    await hint_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    async def _hint_loop(self) -> None:
        """Consume the wake bus; an ``events_appended`` key nudges the
        drain loop out of its poll sleep."""
        sub = self._bus.subscribe()
        try:
            async for event in sub:
                if event.event_key == EVENTS_APPENDED_KEY:
                    self._wake.set()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - hints are best-effort
            logger.warning(
                "event-dispatcher: hint subscription ended", exc_info=True,
            )
        finally:
            closer = getattr(sub, "aclose", None)
            if closer is not None:
                try:
                    await closer()
                except Exception:  # noqa: BLE001
                    pass

    # -- one pass ---------------------------------------------------------

    async def drain_once(self) -> int:
        """Process every unpaused subscription once; return deliveries.

        Public so tests (and fixtures that need synchronous CDC) can
        drain without running the background loop.
        """
        subs_storage = self._sp.get_storage(EventSubscription)
        page = await subs_storage.list(OffsetPage(length=200))
        delivered = 0
        for sub in page.items:
            if sub.paused:
                continue
            try:
                delivered += await self._process(sub)
            except Exception:  # noqa: BLE001 - isolate per subscription
                logger.exception(
                    "event-dispatcher: subscription %s failed", sub.id,
                )
        return delivered

    async def _process(self, sub: EventSubscription) -> int:
        store = self._sp.get_event_store()
        cursor = await store.get_cursor(sub.id)
        if cursor is None:
            # New subscription: consume from now, don't replay history.
            # The row is written even at head 0 so this init happens
            # exactly once per subscription.
            cursor = await store.max_id()
            await store.set_cursor(sub.id, cursor)
        delivered = 0
        while True:
            events = await store.read_after(cursor, limit=self._batch)
            if not events:
                return delivered
            for event in events:
                if matches(event, sub.filter):
                    ok = await self._deliver(sub, event)
                    if ok:
                        delivered += 1
                    else:
                        key = (sub.id, event.id)
                        count = self._fail_counts.get(key, 0) + 1
                        self._fail_counts[key] = count
                        if count < self._max_failures:
                            # Hold the cursor; the next tick retries.
                            await store.set_cursor(sub.id, cursor)
                            return delivered
                        logger.error(
                            "event-dispatcher: skipping event %d for "
                            "subscription %s after %d failures",
                            event.id, sub.id, count,
                        )
                        self._fail_counts.pop(key, None)
                        if self._sub_completed(sub):
                            # A one-shot wake that kept failing is an
                            # orphan (timed-out or cancelled park): GC
                            # it rather than letting it stalk the log.
                            await self._complete_one_shot(sub)
                            return delivered
                    if ok:
                        self._fail_counts.pop((sub.id, event.id), None)
                        if self._sub_completed(sub):
                            # One-shot sink fired: the subscription is
                            # gone, stop reading for it.
                            return delivered
                cursor = event.id
            await store.set_cursor(sub.id, cursor)

    def _sub_completed(self, sub: EventSubscription) -> bool:
        sink = sub.sink
        return isinstance(sink, SessionWakeSink) and sink.one_shot

    # -- sinks ------------------------------------------------------------

    async def _deliver(self, sub: EventSubscription, event: Event) -> bool:
        """Run the sink; True on success. Never raises."""
        try:
            sink = sub.sink
            if isinstance(sink, LogSink):
                logger.info(
                    "event %s id=%d entity=%s/%s actor=%s session=%s",
                    event.event_type, event.id,
                    event.entity_kind, event.entity_id,
                    event.actor, event.session_id,
                )
                return True
            if isinstance(sink, SessionWakeSink):
                return await self._deliver_session_wake(sub, sink, event)
            return await self._deliver_converge(event)
        except Exception:  # noqa: BLE001 - failure counted by caller
            logger.warning(
                "event-dispatcher: sink failed for subscription %s "
                "event %d", sub.id, event.id, exc_info=True,
            )
            return False

    async def _deliver_converge(self, event: Event) -> bool:
        if not event.entity_kind or not event.entity_id:
            return True  # nothing to converge; not a failure
        from primer.knowledge.system_collection import converge_entity

        await converge_entity(
            self._sp,
            entity_type=event.entity_kind,
            entity_id=event.entity_id,
            provider_registry=self._provider_registry,
            semantic_search_registry=self._semantic_search_registry,
        )
        return True

    async def _deliver_session_wake(
        self, sub: EventSubscription, sink: SessionWakeSink, event: Event,
    ) -> bool:
        park = await self._park_state(sink)
        if park == "gone":
            # The session ended or vanished: the wait can never be
            # answered. Consume the event and GC the subscription.
            await self._complete_one_shot(sub)
            return True
        if park == "pending":
            # The event beat the worker's park write (the tool creates
            # the subscription before the park row is durable). Hold
            # the cursor; the next tick re-checks. The failure counter
            # doubles as the orphan GC: a park that resumed by timeout
            # never becomes visible again, and the skip path deletes
            # the one-shot.
            return False
        if self._bus is None:
            logger.warning(
                "event-dispatcher: no bus to wake session %s "
                "(subscription %s); the park's timeout will handle it",
                sink.session_id, sub.id,
            )
            return False
        await self._bus.publish(
            sink.event_key, event.model_dump(mode="json"),
        )
        if sink.one_shot:
            await self._complete_one_shot(sub)
        return True

    async def _park_state(self, sink: SessionWakeSink) -> str:
        """'parked' (deliver), 'pending' (retry later), or 'gone' (GC)."""
        from primer.model.workspace_session import (
            SessionStatus,
            WorkspaceSession,
        )

        try:
            row = await self._sp.get_storage(WorkspaceSession).get(
                sink.session_id,
            )
        except Exception:  # noqa: BLE001 - treat a read hiccup as pending
            logger.warning(
                "event-dispatcher: session %s read failed", sink.session_id,
                exc_info=True,
            )
            return "pending"
        if row is None or row.status == SessionStatus.ENDED:
            return "gone"
        keys = set(row.parked_event_keys or [])
        if row.parked_event_key:
            keys.add(row.parked_event_key)
        if row.parked_status in ("parked", "resumable") and (
            sink.event_key in keys
        ):
            return "parked"
        return "pending"

    async def _complete_one_shot(self, sub: EventSubscription) -> None:
        store = self._sp.get_event_store()
        subs_storage = self._sp.get_storage(EventSubscription)
        try:
            await subs_storage.delete(sub.id)
        except Exception:  # noqa: BLE001 - already gone is fine
            logger.debug(
                "one-shot subscription %s already deleted", sub.id,
            )
        await store.delete_cursor(sub.id)


class EventRetentionPruner(_BackgroundTask):
    """Daily prune of event rows old enough AND behind every cursor."""

    role = ROLE_EVENT_RETENTION

    def __init__(
        self,
        *,
        storage_provider: Any,
        retention_days: int = 30,
        interval_seconds: float = 24 * 3600.0,
    ) -> None:
        super().__init__(name="event-retention")
        self._sp = storage_provider
        self._retention_days = retention_days
        self._interval = interval_seconds

    async def _run(self) -> None:
        while not self._stopping:
            try:
                removed = await self.prune_once()
                if removed:
                    logger.info(
                        "event-retention: pruned %d events", removed,
                    )
            except Exception:  # noqa: BLE001
                logger.exception("event-retention: prune failed")
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                break

    async def prune_once(self) -> int:
        from datetime import datetime, timedelta, timezone

        store = self._sp.get_event_store()
        floor = await store.active_cursor_floor()
        keep_after_id = floor if floor is not None else await store.max_id()
        return await store.prune(
            older_than=(
                datetime.now(timezone.utc)
                - timedelta(days=self._retention_days)
            ),
            keep_after_id=keep_after_id,
        )


__all__ = ["EventDispatcher", "EventRetentionPruner"]
