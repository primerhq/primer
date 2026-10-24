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

from pydantic_core import to_jsonable_python

from primer.int.coordinator import (
    ROLE_HARNESS_SWEEPER,
    ROLE_SESSION_MESSAGING_SWEEPER,
    ROLE_STUCK_SESSION_SWEEPER,
    ROLE_TIMEOUT_SWEEPER,
    ROLE_TIMER_SCHEDULER,
)
from primer.int.claim import ClaimKind
from primer.int.event_bus import EventBus
from primer.int.storage import Storage
from primer.model.except_ import NotFoundError
from primer.model.storage import FieldRef, OffsetPage, Op, OrderBy, Predicate, Value
from primer.model.workspace_session import SessionStatus
from primer.storage import raw_generation
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
        storage_provider=None,
    ) -> None:
        super().__init__(name="yield-timer-scheduler")
        self._bus = bus
        self._storage = session_storage
        self._poll = poll_seconds
        self._sp = storage_provider

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
            if self._sp is not None:
                from primer.events.wake import emit_session_wake

                await emit_session_wake(self._sp, self._bus, event_key, {})
            else:
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
        storage_provider=None,
    ) -> None:
        super().__init__(name="yield-timeout-sweeper")
        self._bus = bus
        self._storage = session_storage
        self._poll = poll_seconds
        self._sp = storage_provider
        # Parked sessions already reported as unwakeable, so a row that stays broken is
        # logged once rather than every tick. Rebuilt each tick, so it stays bounded and
        # a row that is repaired and breaks again is reported again.
        self._reported_unwakeable: set[str] = set()

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
            if self._sp is not None:
                from primer.events.wake import emit_session_wake

                await emit_session_wake(
                    self._sp, self._bus, event_key, payload,
                )
            else:
                await self._bus.publish(event_key, payload=payload)
        await self._report_unwakeable_parks()

    async def _report_unwakeable_parks(self) -> None:
        """Log an ERROR, once per session, for a park nothing here can ever time out.

        The sweeps above select on ``parked_until`` and ``parked_event_key``; a parked row
        missing either is skipped silently, so a park with no deadline would sit forever
        with nothing recording that it is stuck. No current park writer produces one
        (``ParkRequest.parked_until`` is typed optional, and a test pins that), so this is
        a tripwire for a future writer, not a handled case.
        """
        current: set[str] = set()
        async for sess in _iter_parked_sessions(self._storage):
            if sess.parked_until is not None and sess.parked_event_key is not None:
                continue
            current.add(sess.id)
            if sess.id in self._reported_unwakeable:
                continue
            missing = [
                name for name, value in (
                    ("parked_until", sess.parked_until),
                    ("parked_event_key", sess.parked_event_key),
                ) if value is None
            ]
            logger.error(
                "yield-timeout-sweeper: session %s is parked but has no %s, so no "
                "timeout or timer sweep can ever wake it; it will stay parked until "
                "something publishes its event or an operator intervenes",
                sess.id, " and no ".join(missing),
            )
        self._reported_unwakeable = current


#: A session's first turn is claimed moments after the row is created. One that is still at
#: turn 0 well past this AND has no lease row at all lost its claim (the lease was deleted or
#: never armed) and will sit non-terminal forever, because every other terminal path runs
#: INSIDE a turn that never started. A session whose worker died or was OOM-killed keeps its
#: lease row, so it is NOT reaped: the claim loop reclaims it and re-runs the turn.
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
    second concurrent run. The lease table is the signal that actually distinguishes the
    two cases: a started or queued session HAS a lease row (claimed and heartbeated, or
    armed and waiting for a worker, or claimed by a worker that has since died and awaiting
    reclaim), and only a session whose claim was LOST has none. The check is for the row,
    not for a live claim: ``has_live_lease`` is False for an armed lease no worker has
    claimed yet, so a fresh first turn waiting more than ten minutes behind a saturated
    pool used to be ended while its lease sat in the queue. A session whose worker died
    keeps its row, and the claim loop reclaims it and re-runs the turn, which is the
    system's ordinary at-least-once recovery; ending it here would defeat that.
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
            # No lock: the sweeper is leader-elected and can run on another pod than the API that wakes a session, so a process-local lifecycle
            # lock would not fence it, and a holder doing workspace I/O would stall every tick. The fences are in the data (see ``_reap``).
            if await self._reap(row.id):
                reaped += 1
        return reaped

    async def _reap(self, session_id: str) -> bool:
        """Re-read ``session_id`` and end it if it is still a never-started (or unfinished-failure) row with no lease. True when it was ended."""
        fresh = await self._storage.get(session_id)
        # Re-read before writing: the claim may have landed while we were looking, in
        # which case the turn is now running and must not be ended.
        if fresh is None or not _never_started(fresh, self._grace):
            return False
        # The decisive check, and the last one before a destructive write: a lease
        # row means the session is running, queued, or awaiting reclaim; only a lost
        # claim has none. Done per candidate rather than as a bulk filter because the
        # candidate list is already narrow (turn_no == 0 past the grace) and the read
        # must be as close to the write as possible.
        if await self._has_lease(fresh.id):
            return False
        # ONE field-scoped write of the four fields this sweeper owns, fenced on what the decision above depended on: a
        # park, a finished first turn or another ender that landed since the read (the lease lookup awaits) is not
        # overwritten, and the first terminal reason wins. A whole-row update from the snapshot would have erased it.
        where = {
            "status": [raw_generation(fresh, "status")],
            "turn_no": [raw_generation(fresh, "turn_no")],
            "parked_status": [raw_generation(fresh, "parked_status")],
        }
        if fresh.last_turn_error is None:
            # a failure stamped between the read and this write makes the row one this sweeper must leave to the failure exit
            where["last_turn_error"] = [None]
        else:
            # A stamped row it DID read, still RUNNING, is the reapable half-finished failure exit. A wake or a resume of a rested failure
            # restarts ``started_at`` (it is what the age check above reads), so a message that landed since the read, on ANY pod, refuses the
            # write.
            where["started_at"] = [raw_generation(fresh, "started_at")]
        # A stamped row that is still RUNNING did start: its failure exit stamped it and then did not finish. It is ended for that, not
        # for a first turn that never ran.
        unfinished = fresh.last_turn_error is not None
        written = await self._storage.patch_if(
            fresh.id,
            to_jsonable_python({
                "status": SessionStatus.ENDED,
                "ended_reason": "failed",
                "ended_detail": "failure_exit_unfinished" if unfinished else "never_started",
                "ended_at": datetime.now(timezone.utc),
            }),
            where=where,
        )
        if written is None:
            logger.info(
                "stuck-session-sweeper: %s changed since it was read (a park, a finished first turn, a failure stamp, a wake or another ender); "
                "left alone", fresh.id,
            )
            return False
        if unfinished:
            logger.warning(
                "stuck-session-sweeper: ended %s - its first turn failed (%s) and the failure exit never finished",
                fresh.id, fresh.last_turn_error.code,
            )
        else:
            logger.warning(
                "stuck-session-sweeper: ended %s - created %s, first turn never ran",
                fresh.id, fresh.created_at,
            )
        return True

    async def _has_lease(self, session_id: str) -> bool:
        """Whether *session_id* still has a SESSION lease row (running, queued or reclaimable).

        Errs toward "yes" on both no-engine and error paths. Skipping a genuinely stuck
        session costs one more poll interval; ending a live one destroys a running job.
        """
        if self._claim_engine is None:
            return True
        try:
            return await self._claim_engine.has_lease(ClaimKind.SESSION, session_id)
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
    """Whether *session* is non-terminal and its first turn never ran, past the grace.

    A session PARKED in its first turn did start. A park neither bumps ``turn_no`` (that is
    bumped only by a completed turn) nor leaves a lease behind (the park releases it), so
    such a row is indistinguishable from a never-started one by the two checks this
    function and its caller make: ``turn_no == 0`` and no lease row. Without this guard a
    first turn waiting on a human approval, ``ask_user`` or a timer for ten minutes was
    ended ``failed`` / ``never_started`` while the human was still deciding. A park has its
    own bound: the dispatch and resume park writers all set ``parked_until``, and
    ``TimeoutSweeper`` / ``TimerScheduler`` wake it when that passes.

    So did a first turn that FAILED and left the session resting (``last_turn_error``, C-024): it is not selected unless its row is still RUNNING, which
    means its failure exit stamped it and never finished (see the guard below).
    """
    if session.status == SessionStatus.ENDED:
        return False
    if session.turn_no > 0:
        return False
    if session.parked_status is not None:
        return False
    if session.last_turn_error is not None and session.status != SessionStatus.RUNNING:
        # A first turn that FAILED and left the session resting (C-024): a failed turn does not bump turn_no and its release drops the lease, so
        # it has every other mark of a session that never started. The row says it did. Every row that is not RUNNING: a resting row is WAITING,
        # and the user can pause it (``pause_session`` moves WAITING to PAUSED and keeps the stamp). A stamped row that is still RUNNING is one whose
        # failure exit did not finish (the status transition raised) and nothing else would end it, so it stays reapable.
        return False
    ref = session.started_at or session.created_at
    if ref is None:
        return False
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - ref).total_seconds()
    return age >= grace_seconds



class SessionMessagingSweeper(_BackgroundTask):
    """Delivers the queued messages of a session that nothing else will (audit G7-14), and trims the peer-message ledger (audit G7-15).

    A message that waits in a session's pending queue (``PendingSessionMessage``) is realized at a turn boundary: the drain checkpoint of the
    turn that ends, the pool's ``_end_session``, the completed-turn guard. A session that comes to rest with messages still queued and no turn
    to come (the worker died between the turn's terminal write and its drain, a realize whose wake failed and left the message queued, a
    message queued behind older ones on an idle session) used to keep them until some later message arrived, which then ran first. Each tick,
    and first when the task takes the role (at boot on the first process to win it), this realizes the queue head of every such session
    through the same ``realize_next_pending`` a turn boundary uses: the conditional arm of ``wake_session`` (one turn per wake), and the row
    deleted only after its wake lands. One message per session per tick, as a checkpoint does; the turn it starts drains the rest.

    A session qualifies when it is live and at rest: CREATED, RUNNING or WAITING, ``turn_status`` idle, not parked, no pause requested (a
    paused session holds its queue on purpose; an ENDED one is reopened by the next message, after which its turn boundary drains it), and with
    no SESSION lease row. A lease row means a turn is running, queued or awaiting reclaim, and that turn's own boundary drains the queue, so
    the sweep never races a drain on another process. A queued row whose session no longer exists is deleted: nothing can deliver it.

    Each tick first deletes the peer-message ledger rows (``PeerMessage``) older than ``PEER_LEDGER_TTL``: the rate check counts only rows
    inside its window, and the per-sender trim runs only when that sender sends again, so the rows of a session that ended or was deleted
    (the delete route takes a session's own rows with it) were kept for ever.

    Leader-elected (``ROLE_SESSION_MESSAGING_SWEEPER``), so one process sweeps at a time. Without a claim engine it cannot tell a session a
    turn holds from one at rest, and realizes nothing (it still trims the ledger).
    """

    role = ROLE_SESSION_MESSAGING_SWEEPER

    def __init__(
        self,
        *,
        storage_provider,
        scheduler,
        claim_engine: "ClaimEngine | None",
        workspace_registry,
        event_bus: EventBus | None = None,
        poll_seconds: float = DEFAULT_SWEEPER_POLL_SECONDS,
        stale_arm_grace_seconds: float | None = None,
    ) -> None:
        super().__init__(name="session-messaging-sweeper")
        self._sp = storage_provider
        self._scheduler = scheduler
        self._claim_engine = claim_engine
        self._workspace_registry = workspace_registry
        self._bus = event_bus
        self._poll = poll_seconds
        # None: derived from the configured message write timeout when the sweep runs (``persistence.stale_arm_grace_seconds``).
        self._stale_arm_grace = stale_arm_grace_seconds
        # Rows seen claimable with no claim: id -> (turn_status, last_seq, first seen on the loop clock).
        self._unclaimed: dict[str, tuple[str, int, float]] = {}

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self._tick()
            except Exception as exc:  # noqa: BLE001 - a sweeper must never die on one bad row
                logger.exception("session-messaging-sweeper: tick failed: %s", exc)
            try:
                await asyncio.sleep(self._poll)
            except asyncio.CancelledError:
                break

    async def _tick(self) -> int:
        """Trim the peer-message ledger, heal a turn a wake left armed with no claim, then realize the queue head of every session at rest that
        has one. Returns how many sessions were woken from their queue."""
        await self._trim_peer_ledger()
        if self._claim_engine is None:
            return 0
        try:
            await self._heal_unclaimed_turns()
        except Exception:  # noqa: BLE001 - the queue sweep runs anyway; the rows are looked at again next tick
            logger.exception("session-messaging-sweeper: healing armed turns failed")
        woken = 0
        for session_id in await self._sessions_with_queued_messages():
            try:
                if await self._realize_if_at_rest(session_id):
                    woken += 1
            except Exception:  # noqa: BLE001 - one session must not stop the sweep; its message stays queued for the next tick
                logger.exception("session-messaging-sweeper: realizing the queue of session %s failed", session_id)
        return woken

    async def _trim_peer_ledger(self) -> int:
        """Delete the peer-message ledger rows older than ``PEER_LEDGER_TTL`` (audit G7-15): they count for no rate check, and the per-sender
        trim never reaches the rows of a sender that is gone or stopped sending. Never fails the tick: the queue sweep runs anyway."""
        from primer.session.peer_messages import trim_stale_peer_ledger

        try:
            deleted = await trim_stale_peer_ledger(self._sp, now=datetime.now(timezone.utc))
        except Exception:  # noqa: BLE001 - the rows are kept for the next tick
            logger.exception("session-messaging-sweeper: trimming the peer-message ledger failed")
            return 0
        if deleted:
            logger.info("session-messaging-sweeper: deleted %d stale peer-message ledger row(s)", deleted)
        return deleted

    def _grace(self) -> float:
        if self._stale_arm_grace is not None:
            return self._stale_arm_grace
        from primer.session.persistence import stale_arm_grace_seconds

        return stale_arm_grace_seconds()

    async def _heal_unclaimed_turns(self) -> int:
        """Heal a turn a wake took and never settled, and give a claim to a turn left armed with none (audit G7-03). Returns how many rows
        were healed.

        * ``arming`` (any status): the wake that took the turn stamped ``armed_at``. Past the grace (above a live wake's documented worst case,
          derived from the configured message write timeout: ``persistence.stale_arm_grace_seconds``) the wake is judged dead, WHETHER OR
          NOT the session has a lease row (a claim armed by ``/resume`` or boot recovery is handed back while the turn is arming, so a lease
          proves nothing about the wake; review N2). A PAUSED or ENDED row goes back to ``idle``; any other is decided by the log: a USER_INPUT
          nobody answered (``has_open_turn`` from the drain cursor) makes it ``claimable`` and claimed, none gives it back to ``idle``, and a
          log that cannot be read counts as holding one (a turn that runs with nothing new costs one model call; a message nobody answers
          costs the person). A wake still alive that finds its turn given back takes it again once its message is written.
        * ``claimable`` with no SESSION lease row (a promotion whose claim upsert failed): seen in the same state, at the same ``last_seq``, on
          two ticks at least the grace apart, the claim is armed. A PAUSED row is left to ``/resume``; a row a lease holds is left to its turn.

        Each heal is one ``patch_if`` fenced on what it was judged on. The candidates are read first and healed after, so healing a row never
        shifts the pages being read (review N12).
        """
        from primer.model.workspace_session import WorkspaceSession

        storage = self._sp.get_storage(WorkspaceSession)
        predicate = Predicate(left=FieldRef(name="turn_status"), op=Op.IN, right=Value(value=["arming", "claimable"]))
        candidates = []
        offset = 0
        while True:
            page = await storage.find(predicate, OffsetPage(offset=offset, length=_MAX_PAGE))
            items = list(page.items)
            candidates.extend(items)
            if len(items) < _MAX_PAGE:
                break
            offset += _MAX_PAGE

        grace = self._grace()
        now = asyncio.get_running_loop().time()
        wall_now = datetime.now(timezone.utc)
        seen: dict[str, tuple[str, int, float]] = {}
        healed = 0
        for row in candidates:
            if row.turn_status == "arming":
                armed_at = row.armed_at
                if armed_at is not None:
                    if armed_at.tzinfo is None:
                        armed_at = armed_at.replace(tzinfo=timezone.utc)
                    if (wall_now - armed_at).total_seconds() < grace:
                        continue
                if await self._heal_an_arm(storage, row):
                    healed += 1
                continue
            if row.status in (SessionStatus.PAUSED, SessionStatus.ENDED) or row.parked_status is not None:
                continue
            if await self._has_lease(row.id):
                continue
            key = (row.turn_status, row.last_seq)
            earlier = self._unclaimed.get(row.id)
            first_seen = earlier[2] if earlier is not None and earlier[:2] == key else now
            if now - first_seen < grace:
                seen[row.id] = (*key, first_seen)
                continue
            await self._arm_the_claim(row.id)
            logger.warning("session-messaging-sweeper: session %s was left claimable with no claim; it is claimed now", row.id)
            healed += 1
        self._unclaimed = seen
        return healed

    async def _heal_an_arm(self, storage, row) -> bool:
        """Settle a turn a dead wake left ``arming`` (see :meth:`_heal_unclaimed_turns`)."""
        if row.status in (SessionStatus.PAUSED, SessionStatus.ENDED):
            target = "idle"
        else:
            target = "claimable" if await self._message_is_waiting(row) else "idle"
        try:
            written = await storage.patch_if(
                row.id, {"turn_status": target, "armed_at": None},
                where={"turn_status": ["arming"], "armed_at": [raw_generation(row, "armed_at")]},
            )
        except NotFoundError:
            return False
        if written is None:
            return False
        if target == "claimable":
            await self._arm_the_claim(row.id)
        logger.warning(
            "session-messaging-sweeper: session %s was left arming by a wake that did not settle it; it is now %s%s",
            row.id, target, " and claimed" if target == "claimable" else "",
        )
        return True

    async def _arm_the_claim(self, session_id: str) -> None:
        await self._claim_engine.upsert(ClaimKind.SESSION, session_id)
        try:
            await self._scheduler.enqueue(session_id)
        except Exception:  # noqa: BLE001 - the claim is armed; the scheduler pulse is a hint
            logger.debug("session-messaging-sweeper: scheduler pulse failed for %s", session_id, exc_info=True)

    async def _message_is_waiting(self, row) -> bool:
        """Whether the session's log holds a USER_INPUT no turn has answered yet (from the drain cursor). True when the log cannot be read."""
        from primer.session.dispatch import _read_message_lines
        from primer.session.turns import has_open_turn
        from primer.worker.io_shim import _WorkspaceIOShim

        io = _WorkspaceIOShim(workspace_registry=self._workspace_registry)
        io.register_session(row.id, row.workspace_id)
        lines = await _read_message_lines(io, row)
        if lines is None:
            return True
        return has_open_turn(lines, cursor=row.next_unprocessed_seq)

    async def _sessions_with_queued_messages(self) -> list[str]:
        """The ids of the sessions that have a queued message, oldest queue head first. Paged: the queue is capped per session, but a tick
        that silently read one page would leave the tail stranded while looking like it had swept."""
        from primer.model.workspace_session import PendingSessionMessage

        storage = self._sp.get_storage(PendingSessionMessage)
        order = [OrderBy(field="enqueued_at", direction="asc"), OrderBy(field="id", direction="asc")]
        seen: dict[str, None] = {}
        offset = 0
        while True:
            page = await storage.find(None, OffsetPage(offset=offset, length=_MAX_PAGE), order_by=order)
            items = list(page.items)
            for row in items:
                seen.setdefault(row.session_id, None)
            if len(items) < _MAX_PAGE:
                return list(seen)
            offset += _MAX_PAGE

    async def _realize_if_at_rest(self, session_id: str) -> bool:
        from primer.model.workspace_session import WorkspaceSession
        from primer.session.enqueue import SessionWakeDeps
        from primer.session.pending_messages import drop_queue_of_missing_session, realize_next_pending

        row = await self._sp.get_storage(WorkspaceSession).get(session_id)
        if row is None:
            dropped = await drop_queue_of_missing_session(self._sp, session_id)
            logger.info("session-messaging-sweeper: dropped %d queued message(s) of session %s, which no longer exists", dropped, session_id)
            return False
        if not _at_rest(row) or await self._has_lease(session_id):
            return False
        woken = await realize_next_pending(
            storage_provider=self._sp,
            workspace_id=row.workspace_id,
            session_id=session_id,
            wake_deps=SessionWakeDeps(
                storage_provider=self._sp,
                scheduler=self._scheduler,
                claim_engine=self._claim_engine,
                workspace_registry=self._workspace_registry,
                event_bus=self._bus,
            ),
        )
        if woken:
            logger.warning(
                "session-messaging-sweeper: session %s was at rest with a queued message nothing else would deliver; it is woken with it",
                session_id,
            )
        return woken

    async def _has_lease(self, session_id: str) -> bool:
        """Whether a SESSION lease row exists (a turn running, queued or awaiting reclaim). Errs toward yes: a message left queued costs one
        more tick, a realize racing a turn's own drain costs the order of two messages."""
        try:
            return await self._claim_engine.has_lease(ClaimKind.SESSION, session_id)
        except Exception as exc:  # noqa: BLE001 - an unreadable lease table authorises nothing
            logger.warning("session-messaging-sweeper: lease lookup failed for %s, leaving it alone: %s", session_id, exc)
            return True


#: The statuses a session can be at rest in with a queue only a sweep will deliver.
_AT_REST_STATUSES = (SessionStatus.CREATED, SessionStatus.RUNNING, SessionStatus.WAITING)


def _at_rest(session) -> bool:
    """Whether ``session`` is live, idle, not parked and not being paused: a turn boundary would have realized its queue, and none is coming."""
    return (
        session.status in _AT_REST_STATUSES
        and session.turn_status == "idle"
        and session.parked_status is None
        and not session.pause_requested
    )


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
    "DEFAULT_SWEEPER_POLL_SECONDS",
    "DEFAULT_TIMER_POLL_SECONDS",
    "HarnessSweeper",
    "SessionMessagingSweeper",
    "TimeoutSweeper",
    "TimerScheduler",
]
