"""Postgres backends for the Coordinator primitives.

Activated in distributed mode (Postgres event bus selected). Each
primitive uses storage rows + LISTEN/NOTIFY (where appropriate) to
coordinate across processes.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

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
        announce: "Callable[[str], Awaitable[None]] | None" = None,
    ) -> None:
        self._announce = announce
        self._storage = storage
        self._table = storage.rate_limit_lease_table
        self._lease_id = lease_id
        self._key = key
        self._owner_id = owner_id
        self._released = False
        self._lost = asyncio.Event()
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
                f" WHERE lease_id = $2 AND owner_id = $3 "
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
                ok = await self.heartbeat()
                if not ok:
                    self._lost.set()
                    return
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception(
                "rate-limit heartbeat failed for lease %s", self._lease_id,
            )
            self._lost.set()

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
                await conn.execute(
                    f"DELETE FROM {self._table} WHERE lease_id = $1",
                    self._lease_id,
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


class PostgresRateLimiter(RateLimiter):
    """Storage-backed semaphore. Atomic acquire via INSERT-where-COUNT;
    heartbeat-renewed leases bound crash loss to one TTL window."""

    def __init__(
        self, storage_provider: "StorageProvider", owner_id: str, event_bus: "EventBus | None" = None,
    ) -> None:
        self._storage = storage_provider
        self._owner_id = owner_id
        self._table = storage_provider.rate_limit_lease_table
        self._bus = event_bus

    async def _announce_release(self, key: str) -> None:
        assert self._bus is not None
        await self._bus.publish(_RELEASE_EVENT_PREFIX + key, {})

    def _watch_releases(self, key: str):
        """A subscription to this key's releases, taken BEFORE the first attempt so a release between the attempt and the wait is not missed
        (``None`` with no bus: waiters poll)."""
        if self._bus is None:
            return None
        return self._bus.subscribe(key=_RELEASE_EVENT_PREFIX + key, coalesce=True)

    @staticmethod
    async def _wait(watch, timeout: float) -> None:
        """Sleep until a release is announced, or ``timeout`` (the fallback poll)."""
        if watch is None:
            await asyncio.sleep(min(_POLL_SECONDS, timeout))
            return
        try:
            await asyncio.wait_for(watch.__anext__(), timeout=min(_WAIT_FALLBACK_SECONDS, timeout))
        except (TimeoutError, StopAsyncIteration):
            pass

    async def acquire(
        self, key: str, *, max_concurrency: int,
    ) -> RateLimiterLease:
        watch = self._watch_releases(key)
        try:
            while True:
                lease = await self._try_insert(key, max_concurrency)
                if lease is not None:
                    return lease
                # At limit: wait for a release of this key (or the fallback poll) and retry.
                await self._wait(watch, _WAIT_FALLBACK_SECONDS)
        finally:
            if watch is not None:
                await watch.aclose()

    async def try_acquire(
        self, key: str, *, max_concurrency: int, timeout_s: float,
    ) -> RateLimiterLease | None:
        deadline = asyncio.get_event_loop().time() + timeout_s
        watch = self._watch_releases(key)
        try:
            while True:
                lease = await self._try_insert(key, max_concurrency)
                if lease is not None:
                    return lease
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    return None
                await self._wait(watch, remaining)
        finally:
            if watch is not None:
                await watch.aclose()

    async def _try_insert(
        self, key: str, max_concurrency: int,
    ) -> RateLimiterLease | None:
        lease_id = uuid.uuid4().hex
        async with self._storage.pool.acquire() as conn:
            # The admit decision is COUNT-then-INSERT. Under READ COMMITTED
            # the COUNT subquery does not see other transactions' as-yet
            # uncommitted inserts and does not lock the counted rows, so a
            # burst of concurrent acquires for the same key all read the
            # same sub-limit count and over-admit (observed peak >> cap).
            # Serialise per-key admits with a transaction-scoped advisory
            # lock so the count reflects every committed holder. The lock
            # is keyed by the rate-limit key (hashtext -> int4) and is
            # released automatically at COMMIT/ROLLBACK.
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))", key
                )
                row = await conn.fetchval(
                    f"""
                    INSERT INTO {self._table} (lease_id, key, owner_id, claimed_at, expires_at)
                    SELECT $1, $2, $3, now(), now() + ($4 || ' seconds')::interval
                     WHERE (
                        SELECT COUNT(*) FROM {self._table}
                         WHERE key = $2 AND expires_at > now()
                     ) < $5
                    RETURNING lease_id
                    """,
                    lease_id, key, self._owner_id, str(_LEASE_TTL_SECONDS), max_concurrency,
                )
        if row is None:
            return None
        return _PostgresRateLimiterLease(
            storage=self._storage, lease_id=lease_id, key=key, owner_id=self._owner_id,
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
