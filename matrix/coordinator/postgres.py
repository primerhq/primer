"""Postgres backends for the Coordinator primitives.

Activated in distributed mode (Postgres event bus selected). Each
primitive uses storage rows + LISTEN/NOTIFY (where appropriate) to
coordinate across processes.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

from matrix.int.coordinator import RateLimiter, RateLimiterLease

if TYPE_CHECKING:
    from matrix.int.storage_provider import StorageProvider


logger = logging.getLogger(__name__)

_LEASE_TTL_SECONDS = 60
_HEARTBEAT_INTERVAL_SECONDS = 20


class _PostgresRateLimiterLease(RateLimiterLease):
    def __init__(
        self, *, storage, lease_id: str, key: str, owner_id: str,
    ) -> None:
        self._storage = storage
        self._lease_id = lease_id
        self._key = key
        self._owner_id = owner_id
        self._released = False
        self._lost = asyncio.Event()
        self._heartbeat_task: asyncio.Task | None = asyncio.create_task(
            self._heartbeat_loop(),
            name=f"rl-hb-{lease_id}",
        )

    async def __aenter__(self) -> "_PostgresRateLimiterLease":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.release()

    async def heartbeat(self) -> bool:
        async with self._storage.pool.acquire() as conn:
            row = await conn.fetchval(
                """
                UPDATE rate_limit_lease
                   SET expires_at = now() + ($1 || ' seconds')::interval
                 WHERE lease_id = $2 AND owner_id = $3
                RETURNING lease_id
                """,
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
                    "DELETE FROM rate_limit_lease WHERE lease_id = $1",
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
            row = await conn.fetchval(
                """
                INSERT INTO rate_limit_lease (lease_id, key, owner_id, claimed_at, expires_at)
                SELECT $1, $2, $3, now(), now() + ($4 || ' seconds')::interval
                 WHERE (
                    SELECT COUNT(*) FROM rate_limit_lease
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
