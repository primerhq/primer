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
    ROLE_STUCK_SESSION_SWEEPER,
    ROLE_TIMEOUT_SWEEPER,
    ROLE_TIMER_SCHEDULER,
)
from primer.int.claim import ClaimKind
from primer.int.event_bus import EventBus
from primer.int.storage import Storage
from primer.model.storage import FieldRef, OffsetPage, Op, Predicate, Value
from primer.model.workspace_session import SessionStatus
from primer.worker.yield_runtime import make_timeout_payload

if TYPE_CHECKING:
    from primer.int.claim import ClaimEngine
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


#: A session's first turn is claimed moments after the row is created. One that is still at
#: turn 0 well past this never got claimed - the worker died, the node OOM-killed it, the
#: claim was lost - and it will sit non-terminal forever, because every other terminal path
#: runs INSIDE a turn that never started.
STUCK_SESSION_GRACE_SECONDS = 600.0

#: OffsetPage caps a single page at 200 rows.
_MAX_PAGE = 200


class StuckSessionSweeper(_BackgroundTask):
    """Ends sessions that were created but whose first turn never ran.

    Nothing else reaps these. ``TimeoutSweeper`` only handles parks (a turn that started and
    is waiting), and ``cancel`` merely sets a flag the worker reads on its next step - with no
    turn running there is nobody to read it, so a stuck row cannot even be cancelled.

    Left alone, one such row is not merely litter: a ``parallelism="skip"`` subscription
    declines to fire while any attributed session is non-terminal, so a single stuck session
    silently halts its trigger - observed in production as a cron job that stopped for 14h
    with no error recorded anywhere.

    Sessions that HAVE started are never touched however long they run: a turn may
    legitimately take hours, and ending it from underneath the worker would be far worse
    than leaving it alone.

    Establishing "has started" needs the claim engine, and getting this wrong is what the
    ``claim_engine`` argument exists to prevent. ``turn_no`` alone cannot answer it: the
    counter is bumped on RELEASE, so a first turn that has been running for hours still
    reads 0 and looks identical to one that was never claimed. Gating on ``turn_no == 0``
    plus a 10-minute age therefore reaped live sessions — observed on a daily rating job
    whose turns run ~3.5h: the row was flipped to ENDED at the 10-minute mark while its
    worker computed happily for another three hours, which both lied about session state
    and released the ``parallelism="skip"`` gate, letting the next cron tick start a
    second concurrent run. A live lease (heartbeated by the worker, expiring within one
    TTL of its death) is the signal that actually distinguishes the two cases.
    """

    role = ROLE_STUCK_SESSION_SWEEPER

    def __init__(
        self,
        *,
        session_storage: Storage,
        claim_engine: "ClaimEngine | None" = None,
        poll_seconds: float = DEFAULT_SWEEPER_POLL_SECONDS,
        grace_seconds: float = STUCK_SESSION_GRACE_SECONDS,
    ) -> None:
        super().__init__(name="stuck-session-sweeper")
        self._storage = session_storage
        self._claim_engine = claim_engine
        self._poll = poll_seconds
        self._grace = grace_seconds

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self._tick()
            except Exception as exc:  # noqa: BLE001 - a sweeper must never die on one bad row
                logger.exception("stuck-session-sweeper: tick failed: %s", exc)
            try:
                await asyncio.sleep(self._poll)
            except asyncio.CancelledError:
                break

    async def _tick(self) -> int:
        """End every never-started session past the grace period. Returns how many."""
        reaped = 0
        for row in await self._find_stuck():
            fresh = await self._storage.get(row.id)
            # Re-read before writing: the claim may have landed while we were looking, in
            # which case the turn is now running and must not be ended.
            if fresh is None or not _never_started(fresh, self._grace):
                continue
            # The decisive check, and the last one before a destructive write: a live
            # lease means a worker is mid-turn on this session right now. Done per
            # candidate rather than as a bulk filter because the candidate list is
            # already narrow (turn_no == 0 past the grace) and the read must be as
            # close to the write as possible.
            if await self._has_live_lease(fresh.id):
                continue
            await self._storage.update(fresh.model_copy(update={
                "status": SessionStatus.ENDED,
                "ended_reason": "failed",
                "ended_detail": "never_started",
                "ended_at": datetime.now(timezone.utc),
            }))
            reaped += 1
            logger.warning(
                "stuck-session-sweeper: ended %s - created %s, first turn never ran",
                fresh.id, fresh.created_at,
            )
        return reaped

    async def _has_live_lease(self, session_id: str) -> bool:
        """Whether a worker is mid-turn on *session_id*.

        Errs toward "yes" on both no-engine and error paths. Skipping a genuinely stuck
        session costs one more poll interval; ending a live one destroys a running job.
        """
        if self._claim_engine is None:
            return True
        try:
            return await self._claim_engine.has_live_lease(ClaimKind.SESSION, session_id)
        except Exception as exc:  # noqa: BLE001 - an unreadable lease must not authorise a reap
            logger.warning(
                "stuck-session-sweeper: lease lookup failed for %s, leaving it alone: %s",
                session_id, exc,
            )
            return True

    async def _find_stuck(self) -> list:
        """Every never-started session, paged. Pages rather than taking one capped slice:
        a backlog can exceed a page, and a silent truncation would leave the tail stuck
        exactly as before while looking like the sweeper had run."""
        predicate = Predicate(
            left=FieldRef(name="turn_no"),
            op=Op.EQ,
            right=Value(value=0),
        )
        out: list = []
        offset, page_size = 0, _MAX_PAGE
        while True:
            page = await self._storage.find(
                predicate, OffsetPage(offset=offset, length=page_size),
            )
            out.extend(s for s in page.items if _never_started(s, self._grace))
            if len(page.items) < page_size:
                return out
            offset += page_size


def _never_started(session, grace_seconds: float) -> bool:
    """Whether *session* is non-terminal and its first turn never ran, past the grace."""
    if session.status == SessionStatus.ENDED:
        return False
    if session.turn_no > 0:
        return False
    ref = session.started_at or session.created_at
    if ref is None:
        return False
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - ref).total_seconds()
    return age >= grace_seconds


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


# Window size for the parked-session sweeps. Both sweeps must consider
# EVERY parked session, not just the first 200 -- a fixed cap silently
# left parks beyond it stuck (a timer never woken, a deadline never
# timed out). We page through all parked rows; only one window is held
# in storage per round-trip so memory stays bounded.
_PARKED_PAGE_SIZE = 200


async def _iter_parked_sessions(session_storage: Storage):
    """Yield every session with ``parked_status == 'parked'``, paging through
    all rows so nothing past a single fixed cap is dropped."""
    predicate = Predicate(
        left=FieldRef(name="parked_status"),
        op=Op.EQ,
        right=Value(value="parked"),
    )
    offset = 0
    while True:
        page = await session_storage.find(
            predicate, OffsetPage(offset=offset, length=_PARKED_PAGE_SIZE)
        )
        for sess in page.items:
            yield sess
        if len(page.items) < _PARKED_PAGE_SIZE:
            break
        offset += _PARKED_PAGE_SIZE


async def _find_due_timer_keys(session_storage: Storage) -> list[str]:
    """Find ``timer:*`` parked event_keys whose deadline is due (all rows)."""
    now = datetime.now(timezone.utc)
    return [
        sess.parked_event_key
        async for sess in _iter_parked_sessions(session_storage)
        if (
            sess.parked_event_key is not None
            and sess.parked_event_key.startswith("timer:")
            and sess.parked_until is not None
            and sess.parked_until <= now
        )
    ]


async def _find_expired_non_timer_keys(session_storage: Storage) -> list[str]:
    """Find non-``timer:`` parked event_keys whose deadline elapsed (all rows).

    These are the parks whose external event never fired -- the
    sweeper publishes a timeout marker so the resume hook produces
    a YieldTimeout result.
    """
    now = datetime.now(timezone.utc)
    return [
        sess.parked_event_key
        async for sess in _iter_parked_sessions(session_storage)
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
