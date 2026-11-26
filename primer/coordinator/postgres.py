"""Postgres backends for the Coordinator primitives.

Activated in distributed mode (Postgres event bus selected). Each
primitive uses storage rows + LISTEN/NOTIFY (where appropriate) to
coordinate across processes.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from primer.coordinator.wait_signal import waiting_for_slot
from primer.int.coordinator import (
    InvalidationBus,
    InvalidationSubscription,
    InvalidationTopic,
    LeaderElector,
    LeadershipLease,
    RateLimiter,
    RateLimiterLease,
)

if TYPE_CHECKING:
    from primer.int.event_bus import EventBus
    from primer.int.storage_provider import StorageProvider


logger = logging.getLogger(__name__)

_LEASE_TTL_SECONDS = 60
_HEARTBEAT_INTERVAL_SECONDS = 20
# A waiter at the limit sleeps until a release of its key is announced on the bus; the poll is only the fallback for a lost announcement (M10).
_RELEASE_EVENT_PREFIX = "rate_limit_released:"
_WAIT_FALLBACK_SECONDS = 1.0
# With no bus to announce on, the old fixed poll.
_POLL_SECONDS = 0.1


class _PostgresRateLimiterLease(RateLimiterLease):
    def __init__(
        self, *, storage, lease_id: str, key: str, owner_id: str,
        max_concurrency: int,
        announce: "Callable[[str], Awaitable[None]] | None" = None,
    ) -> None:
        self._announce = announce
        self._storage = storage
        self._table = storage.rate_limit_lease_table
        self._lease_id = lease_id
        self._key = key
        self._owner_id = owner_id
        self._max_concurrency = max_concurrency
        self._released = False
        self._lost = asyncio.Event()
        # The replacement row a re-acquire is inserting: recorded BEFORE the insert so a release (or a cancel) mid-insert still deletes it.
        self._pending_id: str | None = None
        # Heartbeat is started lazily on __aenter__ so a lease constructed
        # but never entered (e.g. cancelled between _try_insert returning
        # and the caller's async-with) doesn't leak a daemon heartbeat task
        # that keeps the row alive forever. The row's own 60s TTL still
        # protects against full leak via the sweeper.
        self._heartbeat_task: asyncio.Task | None = None

    async def __aenter__(self) -> "_PostgresRateLimiterLease":
        if self._heartbeat_task is None and not self._released:
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(),
                name=f"rl-hb-{self._lease_id}",
            )
        return self

    async def __aexit__(self, *exc) -> None:
        await self.release()

    async def heartbeat(self) -> bool:
        async with self._storage.pool.acquire() as conn:
            row = await conn.fetchval(
                f"UPDATE {self._table} "
                f"   SET expires_at = now() + ($1 || ' seconds')::interval "
                f" WHERE lease_id = $2 AND owner_id = $3 AND expires_at > now() "
                f"RETURNING lease_id",
                str(_LEASE_TTL_SECONDS), self._lease_id, self._owner_id,
            )
        return row is not None

    async def _heartbeat_loop(self) -> None:
        try:
            while not self._released:
                await asyncio.sleep(_HEARTBEAT_INTERVAL_SECONDS)
                if self._released:
                    return
                try:
                    ok = await self.heartbeat()
                except Exception:
                    # A transient failure (a pool timeout, a blip): the row
                    # is still alive until its TTL lapses, so keep renewing
                    # on the next interval instead of stopping for good.
                    logger.warning(
                        "rate-limit lease %s (key %s) renewal failed; retrying next interval",
                        self._lease_id, self._key,
                    )
                    continue
                if not ok:
                    # The row is gone (swept after its TTL lapsed): the slot
                    # is lost. Nothing here can cancel the call that holds it
                    # (the adapters run it to completion), so the call
                    # proceeds -- loudly logged -- and a replacement row is
                    # requested so the cap holds while it runs.
                    self._lost.set()
                    logger.error(
                        "rate-limit lease %s (key %s) lost; the call keeps running and a replacement slot is being requested",
                        self._lease_id, self._key,
                    )
                    try:
                        new_id = await self._reacquire()
                    except Exception:
                        logger.exception(
                            "rate-limit lease %s (key %s) re-acquire failed; retrying next interval",
                            self._lease_id, self._key,
                        )
                        continue
                    if new_id is None:
                        # The slot is held by someone else: retry on the next tick.
                        continue
                    self._lease_id = new_id
                    self._pending_id = None
                    self._lost = asyncio.Event()  # the slot is ours again
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception(
                "rate-limit heartbeat failed for lease %s", self._lease_id,
            )
            self._lost.set()

    async def _reacquire(self) -> str | None:
        """Request a replacement row for a lost lease. Returns the new lease id, or None when the slot is taken."""
        new_id = uuid.uuid4().hex
        self._pending_id = new_id
        try:
            async with self._storage.pool.acquire() as conn:
                row = await _insert_lease_row(
                    conn, self._table,
                    lease_id=new_id, key=self._key, owner_id=self._owner_id,
                    ttl_seconds=_LEASE_TTL_SECONDS,
                    max_concurrency=self._max_concurrency,
                )
        except Exception:
            # The insert may have committed before the failure (a DB error after a successful insert):
            # remove the row it may have left so it does not sit out its TTL unowned, then re-raise.
            self._pending_id = None
            await self._delete_lease_row(new_id)
            raise
        if row is None:
            self._pending_id = None
        return row

    async def _delete_lease_row(self, lease_id: str) -> None:
        """Best-effort removal of one lease row (a re-acquire that raised after its insert)."""
        try:
            async with self._storage.pool.acquire() as conn:
                await conn.execute(
                    f"DELETE FROM {self._table} WHERE lease_id = $1", lease_id,
                )
        except Exception:
            logger.debug(
                "rate-limit: could not remove orphaned lease row %s; its TTL will reclaim it",
                lease_id, exc_info=True,
            )

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except (asyncio.CancelledError, Exception):
                pass
            self._heartbeat_task = None
        try:
            async with self._storage.pool.acquire() as conn:
                for lease_id in (self._lease_id, self._pending_id):
                    if lease_id is None:
                        continue
                    await conn.execute(
                        f"DELETE FROM {self._table} WHERE lease_id = $1",
                        lease_id,
                    )
                # NOTIFY is best-effort; if it fails we still released the
                # row so other waiters' poll-fallback will pick it up.
                try:
                    await conn.execute(
                        "SELECT pg_notify('rate_limit_released', $1)",
                        self._key,
                    )
                except Exception:
                    pass
            if self._announce is not None:
                try:
                    await self._announce(self._key)
                except Exception:  # best effort: the waiters' fallback poll finds the free slot
                    logger.debug("rate-limit release announce failed for %s", self._key, exc_info=True)
        except Exception:
            logger.exception(
                "rate-limit lease %s release failed", self._lease_id,
            )


async def _insert_lease_row(
    conn, table: str, *, lease_id: str, key: str, owner_id: str,
    ttl_seconds: int, max_concurrency: int,
) -> str | None:
    """One admission attempt: COUNT-then-INSERT under the per-key advisory lock.
    Returns the lease id, or None when the key is at the caller's limit.

    Under READ COMMITTED the COUNT subquery does not see other transactions'
    as-yet uncommitted inserts and does not lock the counted rows, so a burst
    of concurrent acquires for the same key all read the same sub-limit count
    and over-admit (observed peak >> cap). Serialise per-key admits with a
    transaction-scoped advisory lock so the count reflects every committed
    holder. The lock is keyed by the rate-limit key (hashtext -> int4) and is
    released automatically at COMMIT/ROLLBACK.
    """
    async with conn.transaction():
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext($1))", key
        )
        return await conn.fetchval(
            f"""
            INSERT INTO {table} (lease_id, key, owner_id, claimed_at, expires_at)
            SELECT $1, $2, $3, now(), now() + ($4 || ' seconds')::interval
             WHERE (
                SELECT COUNT(*) FROM {table}
                 WHERE key = $2 AND expires_at > now()
              ) < $5
            RETURNING lease_id
            """,
            lease_id, key, owner_id, str(ttl_seconds), max_concurrency,
        )


async def _close_watch(watch: Any) -> None:
    if watch is None:
        return
    try:
        await watch.aclose()
    except Exception:  # noqa: BLE001 -- best effort
        pass


class _Waiter:
    """One in-process caller waiting for a slot under a key."""

    __slots__ = ("future", "limit")

    def __init__(self, future: "asyncio.Future[RateLimiterLease]", limit: int) -> None:
        self.future = future
        self.limit = limit


class _KeyQueue:
    """The in-process waiters for one key, served in arrival order.

    ONE poller task per key waits for a release (the bus announcement, or the
    fallback poll) and then runs the admission pass, so a key with N queued
    callers costs one admission attempt per release, not one per waiter per
    wake. Cross-process order is whatever the Postgres advisory lock grants:
    best effort.
    """

    def __init__(self, key: str, limiter: "PostgresRateLimiter") -> None:
        self.key = key
        self._limiter = limiter
        self.waiters: deque[_Waiter] = deque()
        # Serialises the admission passes with the queue bookkeeping: the
        # poller's empty check and a waiter's enqueue must not interleave, or
        # a waiter could join after the poller decided to leave.
        self._lock = asyncio.Lock()
        self._poller: "asyncio.Task[None] | None" = None
        self._kick = asyncio.Event()
        self._watch: Any = None

    def _ensure_poller_locked(self, watch: Any) -> bool:
        """Start the poller if it is not running. Returns True when it took ``watch``."""
        if self._poller is None or self._poller.done():
            self._poller = asyncio.create_task(
                self._poll(watch), name=f"rl-wait-{self.key}",
            )
            return True
        return False

    async def enqueue(self, waiter: _Waiter, watch: Any = None) -> RateLimiterLease:
        """Register ``waiter`` behind the current line and wait for its admission.

        ``watch`` is a release subscription the caller took before its fast-path attempt; the poller adopts it
        (or it is closed when a poller is already running)."""
        consumed = False
        try:
            async with self._lock:
                consumed = self._ensure_poller_locked(watch)
                self.waiters.append(waiter)
        finally:
            if not consumed:
                await _close_watch(watch)
        try:
            with waiting_for_slot():
                return await waiter.future
        except BaseException:
            if waiter.future.done() and not waiter.future.cancelled() and waiter.future.exception() is None:
                # Granted in the same tick the caller was cancelled: nobody will use the slot, so hand it back.
                await asyncio.shield(waiter.future.result().release())
            async with self._lock:
                try:
                    self.waiters.remove(waiter)
                except ValueError:
                    pass  # already admitted (the pass removed it)
                self._kick.set()  # wake the poller so it sees an empty (or shrunken) line
            raise

    async def _wait_for_wake(self) -> None:
        """Sleep until a release is announced, a waiter joins or leaves (the kick), or the fallback poll fires (the lost-announcement backstop)."""
        if self._watch is None:
            await asyncio.sleep(_POLL_SECONDS)
            return
        kick = asyncio.ensure_future(self._kick.wait())
        announce = asyncio.ensure_future(self._watch.__anext__())
        try:
            await asyncio.wait(
                {kick, announce},
                timeout=_WAIT_FALLBACK_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (kick, announce):
                if not task.done():
                    task.cancel()
            for task in (kick, announce):
                try:
                    await task
                except BaseException:  # noqa: BLE001 -- a cancelled pending __anext__ or wait is expected
                    pass
        self._kick.clear()

    async def _poll(self, watch: Any) -> None:
        self._watch = watch
        if watch is None:
            try:
                self._watch = self._limiter._watch_releases(self.key)
            except Exception:
                logger.exception(
                    "rate-limit release watch failed for key %s; the fallback poll covers it",
                    self.key,
                )
                self._watch = None
        try:
            while True:
                async with self._lock:
                    if not self.waiters:
                        self._poller = None
                        self._limiter._drop_empty_queue(self)
                        return
                await self._wait_for_wake()
                await self._limiter._admit_pass(self)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("rate-limit wait queue failed for key %s", self.key)
        finally:
            if self._watch is not None:
                try:
                    await self._watch.aclose()
                except Exception:  # noqa: BLE001 -- best effort
                    pass


class PostgresRateLimiter(RateLimiter):
    """Storage-backed semaphore. Atomic acquire via INSERT-where-COUNT;
    heartbeat-renewed leases bound crash loss to one TTL window.

    In-process waiters line up per key (FIFO) behind ONE poller, so the
    database sees one admission attempt per release instead of one per
    waiter; the bus announcement wakes the poller and the fallback poll
    covers a lost one (M10).
    """

    def __init__(
        self, storage_provider: "StorageProvider", owner_id: str, event_bus: "EventBus | None" = None,
    ) -> None:
        self._storage = storage_provider
        self._owner_id = owner_id
        self._table = storage_provider.rate_limit_lease_table
        self._bus = event_bus
        self._queues: dict[str, _KeyQueue] = {}

    async def _announce_release(self, key: str) -> None:
        assert self._bus is not None
        await self._bus.publish(_RELEASE_EVENT_PREFIX + key, {})

    def _watch_releases(self, key: str):
        """A subscription to this key's releases, taken by the key's poller before its first wake
        (``None`` with no bus: the poller uses the fixed poll)."""
        if self._bus is None:
            return None
        return self._bus.subscribe(key=_RELEASE_EVENT_PREFIX + key, coalesce=True)

    def _drop_empty_queue(self, queue: "_KeyQueue") -> None:
        """Remove an emptied queue (its poller has exited) so ``_queues`` does not grow without
        bound over a long-running process (review 2026-11-23, G). Called under the queue's lock;
        the identity check drops it only if it is still the current queue for its key."""
        if self._queues.get(queue.key) is queue:
            del self._queues[queue.key]

    async def acquire(
        self, key: str, *, max_concurrency: int,
    ) -> RateLimiterLease:
        queue = self._queues.get(key)
        watch = None
        if queue is None or not queue.waiters:
            # Fast path with no in-process line to join. A caller that
            # arrives while others are queued joins the line instead, so
            # admission stays in arrival order within this process.
            # Subscribe BEFORE the attempt: a release between a failed
            # attempt and the line's first wait would otherwise be missed
            # until the fallback poll.
            try:
                watch = self._watch_releases(key)
            except Exception:
                logger.exception("rate-limit release watch failed for key %s; the fallback poll covers it", key)
            try:
                lease = await self._try_insert(key, max_concurrency)
            except BaseException:
                await _close_watch(watch)
                raise
            if lease is not None:
                await _close_watch(watch)
                return lease
            # Look the line up again: callers that arrived together all saw no queue, and each building its own would split the line.
            queue = self._queues.setdefault(key, _KeyQueue(key, self))
        waiter = _Waiter(asyncio.get_running_loop().create_future(), max_concurrency)
        return await queue.enqueue(waiter, watch)

    async def try_acquire(
        self, key: str, *, max_concurrency: int, timeout_s: float,
    ) -> RateLimiterLease | None:
        if timeout_s <= 0:
            # The established semantics: one immediate attempt, no waiting.
            return await self._try_insert(key, max_concurrency)
        try:
            async with asyncio.timeout(timeout_s):
                return await self.acquire(key, max_concurrency=max_concurrency)
        except TimeoutError:
            return None

    async def _admit_pass(self, queue: _KeyQueue) -> None:
        """Admit waiters in arrival order, one slot per successful insert, until an insert fails.

        A coalesced release (several slots freed in one wake) admits as many waiters as there are
        slots instead of one, so the rest do not wait for the fallback poll (review 2026-11-23, G).
        The attempts stay bounded by the slots freed, not the line: the first failing insert (no
        slot for that waiter) stops the pass, so a long line of equal-limit callers costs one
        attempt per admitted waiter plus the one that finds the limit - not one per waiter.

        The queue lock is held per admission, not across the whole pass (review 2026-11-23, X1 M1):
        a coalesced release of N slots otherwise holds the lock for N DB round trips, and a caller
        on the same key blocks in ``enqueue`` (the same lock) for the whole pass, so a short
        timeout ``try_acquire`` can lapse while a slot was in fact free.
        """
        while True:
            async with queue._lock:
                head = None
                for waiter in list(queue.waiters):
                    if waiter.future.done():
                        queue.waiters.remove(waiter)
                        continue
                    head = waiter
                    break
                if head is None:
                    return  # line empty (its poller's next empty check ends it)
            # One DB round trip with the lock released: a caller on the same key
            # can enqueue (or lapse a short timeout) while this insert is in flight.
            try:
                lease = await self._try_insert(queue.key, head.limit)
            except Exception:
                logger.warning(
                    "rate-limit admission attempt failed for key %s; the next wake retries",
                    queue.key,
                )
                return
            if lease is None:
                break  # no slot for the head waiter: the line's remaining waiters wait for the next pass
            async with queue._lock:
                try:
                    queue.waiters.remove(head)
                except ValueError:
                    pass  # cancelled meanwhile: its enqueue already removed it
            try:
                head.future.set_result(lease)
            except asyncio.InvalidStateError:
                # The caller cancelled between the insert and the wake:
                # hand the slot back instead of leaking it.
                await lease.release()

    async def _try_insert(
        self, key: str, max_concurrency: int,
    ) -> RateLimiterLease | None:
        lease_id = uuid.uuid4().hex
        async with self._storage.pool.acquire() as conn:
            row = await _insert_lease_row(
                conn, self._table,
                lease_id=lease_id, key=key, owner_id=self._owner_id,
                ttl_seconds=_LEASE_TTL_SECONDS, max_concurrency=max_concurrency,
            )
        if row is None:
            return None
        return _PostgresRateLimiterLease(
            storage=self._storage, lease_id=lease_id, key=key, owner_id=self._owner_id,
            max_concurrency=max_concurrency,
            announce=self._announce_release if self._bus is not None else None,
        )


# ---------------------------------------------------------------------------
# InvalidationBus
# ---------------------------------------------------------------------------


class _PostgresInvalidationSubscription(InvalidationSubscription):
    def __init__(
        self,
        bus: "EventBus",
        topic: "InvalidationTopic",
        handler: Callable[[str], Awaitable[None]],
        on_reconnect: Callable[[], None] | None = None,
    ) -> None:
        self._bus = bus
        self._topic = topic
        self._handler = handler
        # Thread the caller's reconnect hook down to the EventBus
        # subscription: LISTEN/NOTIFY drops events across a reconnect, so
        # this fires when the transport comes back and the subscriber
        # needs to treat its cached invalidation state as stale.
        self._sub = bus.subscribe(on_reconnect=on_reconnect, prefix=f"invalidate:{topic.value}:")
        self._task: asyncio.Task = asyncio.create_task(
            self._run(),
            name=f"invalidation-sub-{topic.value}",
        )
        self._closed = False

    async def _run(self) -> None:
        prefix = f"invalidate:{self._topic.value}:"
        try:
            async for event in self._sub:
                if not event.event_key.startswith(prefix):
                    continue
                key = event.event_key[len(prefix):]
                try:
                    await self._handler(key)
                except Exception:
                    logger.exception(
                        "invalidation handler raised; continuing",
                    )
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception(
                "invalidation subscription loop failed for topic %s",
                self._topic.value,
            )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            await self._sub.aclose()
        except Exception:
            pass


class PostgresInvalidationBus(InvalidationBus):
    """Thin wrapper on the EventBus with topic-key conventions.

    Bus type is irrelevant — wherever the EventBus impl ships events
    cross-process (Postgres LISTEN/NOTIFY in production, in-memory queue
    in tests) the invalidation broadcast inherits that delivery
    guarantee.
    """

    def __init__(self, event_bus: "EventBus") -> None:
        self._bus = event_bus

    async def publish(self, topic: "InvalidationTopic", key: str) -> None:
        await self._bus.publish(
            f"invalidate:{topic.value}:{key}", payload={},
        )

    async def subscribe(
        self,
        topic: "InvalidationTopic",
        handler: Callable[[str], Awaitable[None]],
        *,
        on_reconnect: Callable[[], None] | None = None,
    ) -> _PostgresInvalidationSubscription:
        return _PostgresInvalidationSubscription(
            self._bus, topic, handler, on_reconnect,
        )


# ---------------------------------------------------------------------------
# LeaderElector
# ---------------------------------------------------------------------------


_LEADER_HEARTBEAT_INTERVAL_SECONDS = 10


class _PostgresLeadershipLease(LeadershipLease):
    def __init__(
        self,
        *,
        storage,
        role: str,
        owner_id: str,
        lease_seconds: int,
    ) -> None:
        super().__init__(
            role=role, owner_id=owner_id, lost_event=asyncio.Event(),
        )
        self._storage = storage
        self._table = storage.leader_lease_table
        self._lease_seconds = lease_seconds
        self._released = False
        self._heartbeat_task: asyncio.Task = asyncio.create_task(
            self._heartbeat_loop(), name=f"leader-hb-{role}",
        )

    async def _heartbeat_loop(self) -> None:
        try:
            while not self._released:
                await asyncio.sleep(_LEADER_HEARTBEAT_INTERVAL_SECONDS)
                if self._released:
                    return
                async with self._storage.pool.acquire() as conn:
                    row = await conn.fetchval(
                        f"""
                        UPDATE {self._table}
                           SET expires_at = now() + ($1 || ' seconds')::interval
                         WHERE role = $2 AND owner_id = $3
                        RETURNING role
                        """,
                        str(self._lease_seconds), self.role, self.owner_id,
                    )
                if row is None:
                    self.lost_event.set()
                    return
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception(
                "leader heartbeat failed for role %s", self.role,
            )
            self.lost_event.set()

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._heartbeat_task.cancel()
        try:
            await self._heartbeat_task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            async with self._storage.pool.acquire() as conn:
                await conn.execute(
                    f"DELETE FROM {self._table} WHERE role = $1 AND owner_id = $2",
                    self.role, self.owner_id,
                )
                try:
                    await conn.execute(
                        "SELECT pg_notify('leader_released', $1)",
                        self.role,
                    )
                except Exception:
                    pass
        except Exception:
            logger.exception(
                "leader lease %s/%s release failed", self.role, self.owner_id,
            )


class PostgresLeaderElector(LeaderElector):
    def __init__(self, storage_provider: "StorageProvider", owner_id: str) -> None:
        self._storage = storage_provider
        self._owner_id = owner_id
        self._table = storage_provider.leader_lease_table

    async def try_acquire(
        self, role: str, *, lease_seconds: int = 30,
    ) -> "LeadershipLease | None":
        async with self._storage.pool.acquire() as conn:
            row = await conn.fetchval(
                f"""
                INSERT INTO {self._table} AS ll (role, owner_id, claimed_at, expires_at)
                VALUES ($1, $2, now(), now() + ($3 || ' seconds')::interval)
                ON CONFLICT (role) DO UPDATE
                  SET owner_id   = EXCLUDED.owner_id,
                      claimed_at = EXCLUDED.claimed_at,
                      expires_at = EXCLUDED.expires_at
                  WHERE ll.expires_at < now()
                RETURNING owner_id
                """,
                role, self._owner_id, str(lease_seconds),
            )
        if row != self._owner_id:
            return None
        return _PostgresLeadershipLease(
            storage=self._storage, role=role, owner_id=self._owner_id,
            lease_seconds=lease_seconds,
        )
