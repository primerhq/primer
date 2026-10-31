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
from datetime import datetime
from typing import TYPE_CHECKING, Any

from primer.access.workspaces import workspace_level
from primer.bus.scheduler_tasks import _BackgroundTask
from primer.events.filters import matches_async
from primer.events.recorder import EVENTS_APPENDED_KEY
from primer.events.redaction import redact_event
from primer.events.visibility import without_input
from primer.int.coordinator import ROLE_EVENT_DISPATCHER, ROLE_EVENT_RETENTION
from primer.model.event import (
    Event,
    EventSubscription,
    FlipSink,
    LogSink,
    SessionWakeSink,
)
from primer.model.storage import OffsetPage
from primer.model.yield_ import with_wake_entry, with_wake_park

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from primer.model.workspace_session import WorkspaceSession


class EventDispatcher(_BackgroundTask):
    role = ROLE_EVENT_DISPATCHER

    def __init__(
        self,
        *,
        storage_provider: Any,
        event_bus: Any = None,
        provider_registry: Any = None,
        semantic_search_registry: Any = None,
        claim_engine: Any = None,
        poll_seconds: float = 5.0,
        batch: int = 200,
        max_failures: int = 5,
    ) -> None:
        super().__init__(name="event-dispatcher")
        self._sp = storage_provider
        self._bus = event_bus
        self._provider_registry = provider_registry
        self._semantic_search_registry = semantic_search_registry
        self._claim_engine = claim_engine
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
                if await matches_async(event, sub.filter):
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
            if isinstance(sink, FlipSink):
                return await self._deliver_flip(event)
            return await self._deliver_converge(event)
        except Exception:  # noqa: BLE001 - failure counted by caller
            logger.warning(
                "event-dispatcher: sink failed for subscription %s "
                "event %d", sub.id, event.id, exc_info=True,
            )
            return False

    async def _deliver_flip(self, event: Event) -> bool:
        """Replay-safe wake: flip sessions parked on the wake key.

        The same guarded core the YieldEventListener runs from the
        volatile bus; a wake the listener already delivered is a
        no-op here, and one whose publish was lost is delivered from
        the cursor.
        """
        event_key = (event.payload or {}).get("event_key")
        if not event_key:
            return True  # malformed but consumable; nothing to wake
        from primer.model.workspace_session import WorkspaceSession
        from primer.session.yields import flip_sessions_parked_on

        await flip_sessions_parked_on(
            event_key,
            (event.payload or {}).get("wake_payload") or {},
            session_storage=self._sp.get_storage(WorkspaceSession),
            engine=self._claim_engine,
        )
        return True

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
        park, parked_at, row = await self._park_state(sink, sub.id)
        if park == "gone":
            # The session ended or vanished: the wait can never be
            # answered. Consume the event and GC the subscription.
            await self._complete_one_shot(sub)
            return True
        if park == "orphan":
            # The session waits under this key, but on a wait ANOTHER subscription created: this subscription's own wait timed out and the session waited
            # again under the same tool_call_id. It can never be answered by this subscription, and delivering would decide the later wait with an event
            # its filter never asked for. Consume the event, deliver nothing, collect the subscription (ticket 01a1223f, #702 review N6).
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
        if row is not None and await self._read_blocked(row, event):
            # A revoked grant stops delivery (access-control Phase 2, Task 7):
            # the park's turn person no longer reads the event's workspace.
            # Consume the event (move the cursor past it) and leave the
            # subscription alone: the park's timeout settles the wait, and
            # a later event under a restored grant still may wake it.
            logger.info(
                "event-dispatcher: not delivering event %d to session %s "
                "(subscription %s): the turn person no longer reads %r",
                event.id, sink.session_id, sub.id, event.workspace_id,
            )
            await self._sp.get_event_store().set_cursor(sub.id, event.id)
            return True
        if self._bus is None:
            logger.warning(
                "event-dispatcher: no bus to wake session %s "
                "(subscription %s); the park's timeout will handle it",
                sink.session_id, sub.id,
            )
            return False
        # The envelope becomes the agent's tool result (wait_for_event):
        # mask stored secrets exactly as GET /v1/events does (SEC-04).
        # The envelope names the park the sink just read: a copy delivered after the session re-parked under the same key (another subscription's
        # wait) is refused by the flip instead of becoming the new wait's result (ticket 01a1208d, #702 review B2).
        # What a user entered (a canvas.event's value and data) is removed from EVERY wake, whoever waits (an admin's wait_for_event and canvas_wait
        # included): the flip stamps this envelope on the waiting row, which any user may read, and the resume makes it the run's tool result. The
        # input never leaves the session it belongs to; canvas_wait restores it from that session's own canvas (audit G8-08, rulings on #872).
        await self._bus.publish(
            sink.event_key, with_wake_entry(with_wake_park(without_input(redact_event(event)).model_dump(mode="json"), parked_at), sub.id),
        )
        if sink.one_shot:
            await self._complete_one_shot(sub)
        return True

    async def _park_state(self, sink: SessionWakeSink, subscription_id: str | None = None) -> tuple[str, datetime | None, "WorkspaceSession | None"]:
        """``(state, parked_at, row)``: state is 'parked' (deliver), 'pending' (retry later), 'gone' (GC) or 'orphan' (GC: the wait under this key was created by
        another subscription); ``parked_at`` is the park the row had when it was read (``None`` unless 'parked'); ``row`` is the session row read (``None`` when
        the read failed).

        'orphan' needs both ends to carry an identity: the park's entry for the key names the subscription the tool created (``resume_metadata.subscription_id``)
        and ``subscription_id`` is the one delivering. A park that names none (a wait from an older build) is delivered into by the key alone, as before.
        """
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
            return "pending", None, None
        if row is None or row.status == SessionStatus.ENDED:
            return "gone", None, None
        keys = set(row.parked_event_keys or [])
        if row.parked_event_key:
            keys.add(row.parked_event_key)
        if row.parked_status in ("parked", "resumable") and (
            sink.event_key in keys
        ):
            if subscription_id:
                from primer.model.yield_ import entry_id_of
                from primer.session.pending_gates import enumerate_pending_gates

                owners = [
                    entry_id_of(entry.get("resume_metadata"))
                    for entry in enumerate_pending_gates(row.parked_state or {})
                    if entry.get("event_key") == sink.event_key
                ]
                owners = [owner for owner in owners if owner]
                if owners and subscription_id not in owners:
                    return "orphan", None, None
            return "parked", row.parked_at, row
        return "pending", None, None

    async def _read_blocked(self, row: "WorkspaceSession", event: Event) -> bool:
        """Whether ``event`` must not wake this non-admin park (access-control Phase 2, Task 7).

        The session's turn person (``turn_person``, else ``initiated_by``, else an unattributed user:
        ``session_run_principal``) is judged on the event's workspace with ONE ``workspace_level``
        call, keeping the re-check cheap. Admin persons are never blocked; an event without a
        workspace names no readable workspace, so it never wakes a non-admin park. A storage
        hiccup raises, and the caller's failure path holds the cursor and retries (fail closed).
        """
        from primer.trigger.reach import current_person
        from primer.worker.executor_builders import session_run_principal

        # The role held NOW, capped by the recorded one: a demoted admin's park does not keep reading every workspace.
        person = await current_person(session_run_principal(row), self._sp)
        if person.is_admin:
            return False
        if event.workspace_id is None:
            return True
        return await workspace_level(self._sp, person, event.workspace_id) is None

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
