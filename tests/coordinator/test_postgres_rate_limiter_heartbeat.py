"""1.7/2.3: one failed heartbeat no longer stops renewal for good, and a genuinely lost lease is signalled, logged, and re-acquired (the call itself proceeds -- nothing in the limiter can cancel the holder)."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

import primer.coordinator.postgres as pg


class _NullTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    """A scripted fake pool: UPDATE and INSERT outcomes in call order."""

    def __init__(self) -> None:
        self.heartbeat_results: list = []
        self.insert_results: list = []
        self.heartbeats = 0
        self.inserts = 0
        self.update_ids: list[str] = []
        self.deleted: list[str] = []
        self.expired: set[str] = set()  # rows past expires_at but not yet swept

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetchval(self, sql, *args):
        if "UPDATE" in sql:
            self.heartbeats += 1
            self.update_ids.append(args[1])  # (ttl, lease_id, owner_id)
            if "expires_at > now()" in sql and args[1] in self.expired:
                return None  # the predicate: an expired row is not renewed
            step = self.heartbeat_results.pop(0)
            if isinstance(step, BaseException):
                raise step
            return step
        self.inserts += 1
        return self.insert_results.pop(0)

    async def execute(self, sql, *args):
        if "DELETE" in sql:
            self.deleted.append(args[0])

    def transaction(self):
        return _NullTx()

    @property
    def storage(self):
        return SimpleNamespace(rate_limit_lease_table="rl_leases", pool=self)


def _lease(pool: _FakePool) -> pg._PostgresRateLimiterLease:
    return pg._PostgresRateLimiterLease(
        storage=pool.storage, lease_id="lease-1", key="k", owner_id="o",
        max_concurrency=4,
    )


@pytest.mark.asyncio
async def test_a_failed_heartbeat_retries_on_the_next_interval(monkeypatch):
    monkeypatch.setattr(pg, "_HEARTBEAT_INTERVAL_SECONDS", 0.05)
    pool = _FakePool()
    pool.heartbeat_results = [
        RuntimeError("pool timeout"), "lease-1", "lease-1", "lease-1",
    ]
    lease = _lease(pool)
    async with lease:
        await asyncio.sleep(0.2)  # three or four ticks: the failed one, then renewals
    assert pool.heartbeats >= 3   # renewal did not stop after the first failure
    assert not lease._lost.is_set()  # a blip is not a loss
    assert set(pool.update_ids) == {"lease-1"}
    assert pool.deleted == ["lease-1"]  # the release cleans the original row


@pytest.mark.asyncio
async def test_a_lost_lease_is_signalled_logged_and_reacquired(monkeypatch, caplog):
    monkeypatch.setattr(pg, "_HEARTBEAT_INTERVAL_SECONDS", 0.05)
    pool = _FakePool()
    pool.heartbeat_results = ["lease-1", None, "lease-2", "lease-2"]
    pool.insert_results = ["lease-2"]
    lease = _lease(pool)
    async with lease:
        with caplog.at_level(logging.ERROR):
            await asyncio.sleep(0.2)
    assert lease._lease_id == "lease-2"  # the replacement row is held
    assert pool.inserts == 1
    assert any(
        r.levelno == logging.ERROR and "lost" in r.getMessage()
        for r in caplog.records
    )
    assert pool.update_ids[-1] == "lease-2"  # renewal continues on the replacement id
    assert pool.deleted == ["lease-2"]  # the release cleans the CURRENT row


@pytest.mark.asyncio
async def test_a_lost_lease_retries_the_reacquire_while_the_slot_is_taken(monkeypatch):
    monkeypatch.setattr(pg, "_HEARTBEAT_INTERVAL_SECONDS", 0.05)
    pool = _FakePool()
    pool.heartbeat_results = [None, None, "lease-2", "lease-2"]
    pool.insert_results = [None, "lease-2"]
    lease = _lease(pool)
    async with lease:
        await asyncio.sleep(0.2)
    assert lease._lease_id == "lease-2"
    assert pool.inserts == 2  # the first re-acquire lost the race, the second won
    assert pool.deleted == ["lease-2"]


@pytest.mark.asyncio
async def test_a_renewal_of_an_expired_row_is_a_loss_not_a_revival(monkeypatch):
    monkeypatch.setattr(pg, "_HEARTBEAT_INTERVAL_SECONDS", 0.05)
    pool = _FakePool()
    pool.expired = {"lease-1"}  # a blip longer than the TTL: expired, the sweeper has not run yet
    pool.heartbeat_results = ["lease-1"] * 10 + ["lease-2"] * 10  # what an unconditional UPDATE would answer
    pool.insert_results = ["lease-2"]
    lease = _lease(pool)
    async with lease:
        await asyncio.sleep(0.15)
    assert lease._lease_id == "lease-2"  # went through the re-acquire path, which respects the cap
    assert pool.inserts == 1


@pytest.mark.asyncio
async def test_release_deletes_the_replacement_row_a_cancelled_reacquire_inserted(monkeypatch):
    monkeypatch.setattr(pg, "_HEARTBEAT_INTERVAL_SECONDS", 0.05)
    pool = _FakePool()
    pool.heartbeat_results = [None, None]
    started = asyncio.Event()

    real_fetchval = pool.fetchval

    async def slow_insert(sql, *args):
        if "UPDATE" not in sql:
            started.set()
            await asyncio.sleep(30)  # the insert committed server-side but the call is cancelled before it returns
        return await real_fetchval(sql, *args)

    pool.fetchval = slow_insert  # type: ignore[method-assign]
    lease = _lease(pool)
    async with lease:
        await asyncio.wait_for(started.wait(), 2.0)
    pending = lease._pending_id
    assert pending is not None and pending != "lease-1"
    assert set(pool.deleted) == {"lease-1", pending}  # both rows go: no orphan until the sweeper


@pytest.mark.asyncio
async def test_a_reacquire_that_raises_after_its_insert_cleans_up_the_row(monkeypatch):
    """A DB error after the insert committed must not leave the row to sit out its TTL unowned: the
    stale pending id is cleared and the row is removed best-effort (review 2026-11-23, G)."""
    pool = _FakePool()
    real_fetchval = pool.fetchval

    async def raise_after_insert(sql, *args):
        if "UPDATE" not in sql:
            pool.insert_committed = args[0]  # the insert "committed" this row, then failed
            raise RuntimeError("DB error after insert")
        return await real_fetchval(sql, *args)

    pool.fetchval = raise_after_insert  # type: ignore[method-assign]
    lease = _lease(pool)
    with pytest.raises(RuntimeError):
        await lease._reacquire()
    assert lease._pending_id is None  # the stale pending id was cleared
    assert pool.deleted == [pool.insert_committed]  # the orphaned row was removed best-effort
