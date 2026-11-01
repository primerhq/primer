"""M2: the sessions-by-status counts of the metrics scrape are a table scan, so a scrape reuses them for 10 s."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

from primer.scheduler.postgres import STATUS_COUNT_TTL_SECONDS, PostgresScheduler


class _Conn:
    def __init__(self, rows):
        self.rows = rows
        self.scans = 0

    async def fetchval(self, sql):
        return True

    async def fetch(self, sql):
        self.scans += 1
        return [{"status": s, "n": n} for s, n in self.rows.items()]


def _scheduler(conn, clock):
    @asynccontextmanager
    async def acquire():
        yield conn

    storage = SimpleNamespace(schema="public", pool=SimpleNamespace(acquire=acquire))
    return PostgresScheduler(storage_provider=storage, config=SimpleNamespace(), clock=clock)


async def test_a_scrape_inside_the_ttl_does_not_scan_and_one_after_it_sees_the_new_counts():
    now = [100.0]
    conn = _Conn({"running": 2, "ended": 5})
    scheduler = _scheduler(conn, lambda: now[0])

    first = await scheduler.metrics_db_snapshot()
    for _ in range(5):
        assert await scheduler.metrics_db_snapshot() == first
    assert conn.scans == 1
    assert first == {"primer_sessions_active": {"running": 2, "ended": 5}}

    conn.rows = {"running": 1, "ended": 6}  # a status changed
    now[0] += STATUS_COUNT_TTL_SECONDS - 1
    assert (await scheduler.metrics_db_snapshot())["primer_sessions_active"]["running"] == 2  # still cached
    now[0] += 2
    assert await scheduler.metrics_db_snapshot() == {"primer_sessions_active": {"running": 1, "ended": 6}}
    assert conn.scans == 2


async def test_a_caller_cannot_change_the_cached_counts():
    conn = _Conn({"running": 2})
    scheduler = _scheduler(conn, lambda: 1.0)
    (await scheduler.metrics_db_snapshot())["primer_sessions_active"]["running"] = 99
    assert (await scheduler.metrics_db_snapshot())["primer_sessions_active"]["running"] == 2
