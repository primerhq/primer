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


class _PostgresRateLimiterLease(RateLimiterLease):
    def __init__(
        self, *, storage, lease_id: str, key: str, owner_id: str,
    ) -> None:
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
        except Exception:
            logger.exception(
                "rate-limit lease %s release failed", self._lease_id,
            )


class PostgresRateLimiter(RateLimiter):
    """Storage-backed semaphore. Atomic acquire via INSERT-where-COUNT;
    heartbeat-renewed leases bound crash loss to one TTL window."""

    def __init__(self, storage_provider: "StorageProvider", owner_id: str) -> None:
        self._storage = storage_provider
        self._owner_id = owner_id
        self._table = storage_provider.rate_limit_lease_table

    async def acquire(
        self, key: str, *, max_concurrency: int,
    ) -> RateLimiterLease:
        while True:
            lease = await self._try_insert(key, max_concurrency)
            if lease is not None:
                return lease
            # At limit — wait briefly and retry. Future: LISTEN on
            # rate_limit_released and select on event vs sleep.
            await asyncio.sleep(0.1)

    async def try_acquire(
        self, key: str, *, max_concurrency: int, timeout_s: float,
    ) -> RateLimiterLease | None:
        deadline = asyncio.get_event_loop().time() + timeout_s
        while True:
            lease = await self._try_insert(key, max_concurrency)
            if lease is not None:
                return lease
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return None
            await asyncio.sleep(min(0.1, remaining))

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
        self._sub = bus.subscribe(on_reconnect=on_reconnect)
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
