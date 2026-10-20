"""The wiki version stamp on a live Postgres (pivot P1 task 10): claims that really race.

SQLite has one connection and a write lock, so the claims in ``tests/knowledge/wiki/test_stamp.py``
are serialised before they reach the database. On Postgres every claim holds its own pooled
connection: the first-ever ``create`` races on the primary key, and the compare-and-set races on
the row. Gate: ``PRIMER_TEST_POSTGRES_URL`` (see ``tests/pg_gate.py``); the tests use a throwaway
schema of that database and drop it afterwards.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from urllib.parse import urlparse

import pytest
import pytest_asyncio

from primer.knowledge.wiki.stamp import claim, read_stamp, still_mine
from primer.model.provider import PoolConfig, PostgresConfig
from primer.model.wiki import WIKI_STAMP_ID
from primer.storage.postgres import PostgresStorageProvider
from tests.pg_gate import explicit_port, postgres_marks, require_postgres_url

_WHAT = "the wiki stamp Postgres tests"
pytestmark = [*postgres_marks(_WHAT), pytest.mark.asyncio]

#: Every wait is bounded: a claim that never returns fails the test instead of hanging the lane.
_BOUND_S = 30.0


@pytest_asyncio.fixture
async def provider() -> AsyncIterator[PostgresStorageProvider]:
    """A provider on a throwaway schema of the gated database, dropped afterwards."""
    url = urlparse(require_postgres_url(_WHAT))
    schema = f"wst{uuid.uuid4().hex[:16]}"
    sp = PostgresStorageProvider(PostgresConfig(
        hostname=url.hostname or "localhost",
        port=explicit_port(url),
        username=url.username or "primer",
        password=url.password or "",  # type: ignore[arg-type]
        database=(url.path or "/").lstrip("/") or "postgres",
        db_schema=schema,
        pool=PoolConfig(min_size=1, max_size=4),
    ))
    async with asyncio.timeout(_BOUND_S):
        await sp.initialize()
    try:
        yield sp
    finally:
        async with asyncio.timeout(_BOUND_S):
            async with sp.pool.acquire() as c:
                await c.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            await sp.aclose()


async def test_many_pods_of_one_build_leave_one_winner(provider):
    async with asyncio.timeout(_BOUND_S):
        results = await asyncio.gather(*[claim(provider, app_version="0.6.0", content_hash="a") for _ in range(8)])
    assert results.count(True) == 1
    stamp = await read_stamp(provider)
    assert (stamp.id, stamp.app_version, stamp.content_hash, stamp.superseded) == (WIKI_STAMP_ID, "0.6.0", "a", [])


async def test_builds_racing_to_replace_one_stamp_lose_no_update(provider):
    # Different builds at one app version may each win in turn: a claim that reads the stamp after another
    # claim replaced it sees a build it may replace. What must never happen is two winners replacing the SAME
    # stamp, which would drop a hash from the chain. So every winner is on the record exactly once.
    assert await claim(provider, app_version="0.6.0", content_hash="a")
    digests = [f"b{index}" for index in range(6)]
    async with asyncio.timeout(_BOUND_S):
        results = await asyncio.gather(*[claim(provider, app_version="0.6.0", content_hash=d) for d in digests])
    winners = [d for d, won in zip(digests, results, strict=True) if won]
    stamp = await read_stamp(provider)
    assert winners and stamp.content_hash in winners
    assert len(stamp.superseded) == len(winners) and {stamp.content_hash, *stamp.superseded} == {"a", *winners}
    assert await still_mine(provider, stamp.content_hash)
    assert not any([await still_mine(provider, d) for d in digests if d != stamp.content_hash])


async def test_the_stamp_round_trips_its_timestamp_and_history(provider):
    before = datetime.now(UTC)
    for digest in ("a", "b", "c"):
        assert await claim(provider, app_version="0.6.0", content_hash=digest)
    stamp = await read_stamp(provider)
    assert (stamp.content_hash, stamp.superseded) == ("c", ["b", "a"])
    assert stamp.written_at.tzinfo is not None and before <= stamp.written_at <= datetime.now(UTC)


async def test_a_newer_app_version_replaces_the_stamp_and_an_older_one_cannot_follow(provider):
    assert await claim(provider, app_version="0.6.0", content_hash="x")
    assert await claim(provider, app_version="0.7.0", content_hash="y")
    assert not await claim(provider, app_version="0.6.0", content_hash="z")
    assert not await claim(provider, app_version="0.6.0", content_hash="x")
    stamp = await read_stamp(provider)
    assert (stamp.app_version, stamp.content_hash, stamp.superseded) == ("0.7.0", "y", ["x"])
