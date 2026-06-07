"""Background tasks: timer-driven event publishing + timeout sweeper.

Spec: ``docs/superpowers/specs/2026-05-22-yielding-tools-design.md`` §6.3, §6.4.

These two tasks are the "publishers without external sources" — the
internal drivers that make the timer-based parks (sleep) wake on
schedule and that catch parks whose external event never fires
within the parked_until deadline.

* :class:`TimerScheduler` polls the sessions table for ``timer:*``
  parks whose ``parked_until`` is due (or close to due) and
  publishes an empty event on the bus for each. Wakes only the
  sleep tool's parks today; future timer-style yields can use the
  same prefix.
* :class:`TimeoutSweeper` catches non-timer parks (ask_user,
  watch_files, MCP tasks) whose deadline elapsed without their
  external event firing. Publishes the ``__yield_timeout__``
  marker payload so the resume hook produces a YieldTimeout result.

Both run on a single asyncio task per app. Errors are logged and
the loop continues — neither is critical-path on the happy flow
(real events from the bus + the post-flip session_ready NOTIFY do
the wake), but they're the safety net.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from primer.int.coordinator import (
    ROLE_CHAT_SWEEPER,
    ROLE_HARNESS_SWEEPER,
    ROLE_TIMEOUT_SWEEPER,
    ROLE_TIMER_SCHEDULER,
)
from primer.int.event_bus import EventBus
from primer.int.storage import Storage
from primer.model.storage import FieldRef, OffsetPage, Op, Predicate, Value
from primer.worker.yield_runtime import make_timeout_payload

if TYPE_CHECKING:
    from primer.int.coordinator import LeaderElector


logger = logging.getLogger(__name__)


# Default poll cadence in seconds. Tunable per-task on construction
# if e.g. a deployment wants a sweeper that runs every minute
# instead of every 30s.
DEFAULT_TIMER_POLL_SECONDS = 2.0
DEFAULT_SWEEPER_POLL_SECONDS = 30.0


class _BackgroundTask:
    """Base for background loops. Subclasses set ``role`` and override
    ``_run()`` which runs only while the supervisor holds leadership
    (when an elector is provided).
    """

    role: str = ""  # subclass MUST override when using the elector path

    def __init__(self, *, name: str) -> None:
        self._name = name
        self._task: asyncio.Task | None = None
        self._stopping = False

    def start(self, elector: "LeaderElector | None" = None) -> None:
        """Start the supervisor loop.

        With an elector, work runs only while leadership for ``self.role``
        is held; on loss-of-leadership the work loop is cancelled and the
        supervisor immediately tries to re-acquire.

        Without an elector (legacy callers), the work loop runs
        unconditionally. This path is to be removed once all subclasses
        thread an elector through.
        """
        if self._task is not None:
            return
        if elector is None:
            self._task = asyncio.create_task(self._run(), name=self._name)
            return
        self._task = asyncio.create_task(
            self._supervisor_loop(elector), name=f"supervisor-{self._name}",
        )

    async def _supervisor_loop(self, elector: "LeaderElector") -> None:
        """Race the work loop against lease loss; retry on every
        leadership transition until ``stop()`` is called."""
        retry_seconds = 15.0
        while not self._stopping:
            try:
                lease = await elector.try_acquire(self.role)
            except asyncio.CancelledError:
                return
            except Exception:
                # Postgres unreachable, transient failure, etc. Don't
                # exit the supervisor — back off and retry so the
                # task self-heals once the elector recovers.
                logger.exception(
                    "elector try_acquire raised for role %s; retrying",
                    self.role,
                )
                try:
                    await asyncio.sleep(retry_seconds)
                except asyncio.CancelledError:
                    return
                continue
            if lease is None:
                try:
                    await asyncio.sleep(retry_seconds)
                except asyncio.CancelledError:
                    return
                continue
            work: asyncio.Task | None = None
            lost: asyncio.Task | None = None
            try:
                work = asyncio.create_task(self._run(), name=self._name)
                lost = asyncio.create_task(
                    lease.lost_event.wait(), name=f"{self._name}-lost",
                )
                await asyncio.wait(
                    {work, lost}, return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for t in (work, lost):
                    if t is not None and not t.done():
                        t.cancel()
                        try:
                            await t
                        except (asyncio.CancelledError, Exception):  # noqa: BLE001
                            pass
                try:
                    await lease.release()
                except Exception:  # noqa: BLE001
                    pass

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

    async def _run(self) -> None:  # pragma: no cover — overridden
        raise NotImplementedError


class TimerScheduler(_BackgroundTask):
    """Publishes empty events for timer parks whose deadline is due.

    Wakes the ``sleep`` tool (and any future timer-style yields) by
    NOTIFY-ing the bus when the row's ``parked_until`` <= now. The
    bus listener then flips the parked row to resumable; the worker
    pool wakes via ``session_ready`` and resumes the turn.

    A single instance per app suffices because the listener's
    mark_resumable is idempotent.
    """

    role = ROLE_TIMER_SCHEDULER

    def __init__(
        self,
        *,
        bus: EventBus,
        session_storage: Storage,
        poll_seconds: float = DEFAULT_TIMER_POLL_SECONDS,
    ) -> None:
        super().__init__(name="yield-timer-scheduler")
        self._bus = bus
        self._storage = session_storage
        self._poll = poll_seconds

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self._tick()
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "yield-timer-scheduler: tick failed: %s", exc,
                )
            try:
                await asyncio.sleep(self._poll)
            except asyncio.CancelledError:
                break

    async def _tick(self) -> None:
        """One iteration: find due timer parks, publish events."""
        keys = await _find_due_timer_keys(self._storage)
        for event_key in keys:
            await self._bus.publish(event_key, payload={})


class TimeoutSweeper(_BackgroundTask):
    """Publishes timeout markers for parks past their deadline.

    Catches non-timer parks whose external event never fires.
    Publishes ``__yield_timeout__`` payload so the worker's resume
    classifier synthesises a :class:`YieldTimeout` for the tool's
    resume hook.
    """

    role = ROLE_TIMEOUT_SWEEPER

    def __init__(
        self,
        *,
        bus: EventBus,
        session_storage: Storage,
        poll_seconds: float = DEFAULT_SWEEPER_POLL_SECONDS,
    ) -> None:
        super().__init__(name="yield-timeout-sweeper")
        self._bus = bus
        self._storage = session_storage
        self._poll = poll_seconds

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self._tick()
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "yield-timeout-sweeper: tick failed: %s", exc,
                )
            try:
                await asyncio.sleep(self._poll)
            except asyncio.CancelledError:
                break

    async def _tick(self) -> None:
        """One iteration: find expired non-timer parks, publish."""
        keys = await _find_expired_non_timer_keys(self._storage)
        payload = make_timeout_payload()
        for event_key in keys:
            await self._bus.publish(event_key, payload=payload)


class ChatSweeper(_BackgroundTask):
    """Periodically reclaims chats whose worker died mid-turn.

    Wraps :func:`primer.chat.dispatch.sweep_chats` in the same
    background-task harness used by TimeoutSweeper.
    """

    role = ROLE_CHAT_SWEEPER

    def __init__(
        self,
        *,
        storage_provider,
        scheduler,
        event_bus,
        poll_seconds: float = DEFAULT_SWEEPER_POLL_SECONDS,
    ) -> None:
        super().__init__(name="chat-sweeper")
        self._storage_provider = storage_provider
        self._scheduler = scheduler
        self._event_bus = event_bus
        self._poll = poll_seconds

    async def _run(self) -> None:
        from primer.chat.dispatch import sweep_chats
        while not self._stopping:
            try:
                await sweep_chats(
                    storage_provider=self._storage_provider,
                    scheduler=self._scheduler,
                    event_bus=self._event_bus,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("chat-sweeper: tick failed: %s", exc)
            try:
                await asyncio.sleep(self._poll)
            except asyncio.CancelledError:
                break


class HarnessSweeper(_BackgroundTask):
    """Periodically reclaims harnesses whose worker died mid-operation."""

    role = ROLE_HARNESS_SWEEPER

    def __init__(
        self,
        *,
        storage_provider,
        scheduler,
        event_bus,
        provider_registry=None,
        poll_seconds: float = DEFAULT_SWEEPER_POLL_SECONDS,
    ) -> None:
        super().__init__(name="harness-sweeper")
        self._storage_provider = storage_provider
        self._scheduler = scheduler
        self._event_bus = event_bus
        self._provider_registry = provider_registry
        self._poll = poll_seconds

    async def _run(self) -> None:
        from primer.harness.dispatch import HarnessDispatchDeps, sweep_harnesses
        deps = HarnessDispatchDeps(
            storage_provider=self._storage_provider,
            event_bus=self._event_bus,
            provider_registry=self._provider_registry,
        )
        while not self._stopping:
            try:
                await sweep_harnesses(deps)
            except Exception as exc:  # noqa: BLE001
                logger.exception("harness-sweeper: tick failed: %s", exc)
            try:
                await asyncio.sleep(self._poll)
            except asyncio.CancelledError:
                break


# ===========================================================================
# Storage-based lookup helpers
# ===========================================================================
#
# Both helpers query the session Storage backend directly (the same
# source the YieldEventListener uses). This works across all backends
# (in-memory SQLite, Postgres) without type-dispatching, and correctly
# reflects the post-F10c world where park state is written to session
# storage by the claim adapter, not to the scheduler's _sessions dict.


async def _find_due_timer_keys(session_storage: Storage) -> list[str]:
    """Find ``timer:*`` parked event_keys whose deadline is due."""
    now = datetime.now(timezone.utc)
    predicate = Predicate(
        left=FieldRef(name="parked_status"),
        op=Op.EQ,
        right=Value(value="parked"),
    )
    page = await session_storage.find(predicate, OffsetPage(length=200))
    return [
        sess.parked_event_key
        for sess in page.items
        if (
            sess.parked_event_key is not None
            and sess.parked_event_key.startswith("timer:")
            and sess.parked_until is not None
            and sess.parked_until <= now
        )
    ]


async def _find_expired_non_timer_keys(session_storage: Storage) -> list[str]:
    """Find non-``timer:`` parked event_keys whose deadline elapsed.

    These are the parks whose external event never fired -- the
    sweeper publishes a timeout marker so the resume hook produces
    a YieldTimeout result.
    """
    now = datetime.now(timezone.utc)
    predicate = Predicate(
        left=FieldRef(name="parked_status"),
        op=Op.EQ,
        right=Value(value="parked"),
    )
    page = await session_storage.find(predicate, OffsetPage(length=200))
    return [
        sess.parked_event_key
        for sess in page.items
        if (
            sess.parked_event_key is not None
            and not sess.parked_event_key.startswith("timer:")
            and sess.parked_until is not None
            and sess.parked_until <= now
        )
    ]


__all__ = [
    "ChatSweeper",
    "DEFAULT_SWEEPER_POLL_SECONDS",
    "DEFAULT_TIMER_POLL_SECONDS",
    "HarnessSweeper",
    "TimeoutSweeper",
    "TimerScheduler",
]
