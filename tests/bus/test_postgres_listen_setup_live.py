"""Live-Postgres twin of test_postgres_listen_setup.py: a REAL asyncpg pool.

The LISTEN round trip is about a millisecond on a local server, so the window a
cancel has to land in is too narrow to hit reliably. These tests widen it by
wrapping asyncpg's ``Connection.add_listener`` (the real pool, real connections
and real ``PostgresEventBus`` stay in place), then check the thing that matters
in production: the pool gets its connection back, so ``Pool.close()`` (which
waits for every acquired connection with no timeout) cannot hang shutdown.

Skipped unless PRIMER_TEST_POSTGRES_URL is set. Every pool close is bounded, so
a regression fails in seconds instead of hanging the run.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from urllib.parse import parse_qs, urlparse

import asyncpg.connection
import pytest

from primer.bus.postgres import PostgresEventBus
from primer.model.except_ import ConfigError
from primer.model.provider import PoolConfig, PostgresConfig
from primer.storage.postgres import PostgresStorageProvider

_URL_ENV = "PRIMER_TEST_POSTGRES_URL"

pytestmark = pytest.mark.skipif(
    not os.environ.get(_URL_ENV),
    reason=f"set {_URL_ENV} to run the live PostgresEventBus tests",
)

POOL_MAX = 4


def _config() -> PostgresConfig:
    p = urlparse(os.environ[_URL_ENV])
    if p.scheme not in {"postgres", "postgresql"}:
        raise ConfigError(f"unexpected scheme {p.scheme!r} in {_URL_ENV}")
    schema = parse_qs(p.query).get("schema", ["public"])[0]
    return PostgresConfig(
        hostname=p.hostname or "localhost",
        port=p.port or 5432,
        username=p.username or "postgres",
        password=p.password or "",  # type: ignore[arg-type]
        database=(p.path or "/postgres").lstrip("/") or "postgres",
        db_schema=schema,
        pool=PoolConfig(min_size=1, max_size=POOL_MAX),
    )


def _held(sp) -> int:
    """Connections currently acquired from the pool (leaked LISTEN conns)."""
    return sp.pool.get_size() - sp.pool.get_idle_size()


async def _assert_all_returned(sp, what: str, *, timeout: float = 2.0) -> None:
    """Fail unless the pool gets every connection back within ``timeout``.

    Not an immediate check: asyncpg's release is shielded from cancellation and
    finishes a moment after a cancelled task has already returned. A real leak
    is permanent, so it still fails here."""
    deadline = asyncio.get_running_loop().time() + timeout
    while _held(sp):
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail(f"{what}: the pool is still holding {_held(sp)} LISTEN connection(s)")
        await asyncio.sleep(0.02)


async def _close_pool_bounded(sp) -> None:
    held = _held(sp)
    try:
        await asyncio.wait_for(sp.aclose(), 5.0)
    except TimeoutError:
        sp.pool.terminate()
        pytest.fail(
            f"pool.close() hung: a LISTEN connection was never released ({held} still held)"
        )


@pytest.fixture
async def provider():
    sp = PostgresStorageProvider(_config())
    await sp.initialize()
    yield sp
    await _close_pool_bounded(sp)


@pytest.fixture
def slow_listen(monkeypatch):
    """Every real LISTEN takes 0.5s, and ``entered`` is set when one begins."""
    entered = asyncio.Event()
    original = asyncpg.connection.Connection.add_listener

    async def slow(self, channel, callback, *args, **kwargs):
        entered.set()
        await asyncio.sleep(0.5)
        return await original(self, channel, callback, *args, **kwargs)

    monkeypatch.setattr(asyncpg.connection.Connection, "add_listener", slow)
    return entered


@pytest.mark.parametrize("via", ["aclose", "supervisor.cancel"])
async def test_a_subscription_cancelled_mid_listen_returns_its_pooled_connection(
    provider, slow_listen, via,
):
    bus = PostgresEventBus(provider)
    sub = bus.subscribe()
    try:
        await asyncio.wait_for(slow_listen.wait(), 3.0)  # the LISTEN is in flight

        if via == "aclose":
            await sub.aclose()
        else:
            sub._supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sub._supervisor

        await _assert_all_returned(provider, "after the subscription was cancelled mid-LISTEN")
    finally:
        await bus.aclose()


# asyncpg notes that the connection still had a listener when it was released: the
# second cancel interrupted the UNLISTEN, which is exactly the situation under
# test. The release is still correct, because the pool's reset clears the
# client-side listener registry and runs UNLISTEN *.
@pytest.mark.filterwarnings("ignore:.*is being released to the pool but has.*listener:asyncpg.exceptions.InterfaceWarning")
async def test_a_second_cancel_during_the_unlisten_returns_the_pooled_connection(
    provider, monkeypatch,
):
    """The parked supervisor's exit path: the first cancel starts the cleanup,
    whose UNLISTEN is a round trip on the live connection, and a second cancel
    lands inside it. asyncpg's remove_listener is slowed to make that window
    reachable; the pool and connections are real."""
    entered = asyncio.Event()
    original = asyncpg.connection.Connection.remove_listener

    async def slow(self, channel, callback):
        entered.set()
        await asyncio.sleep(0.5)  # the UNLISTEN round trip, still in flight
        return await original(self, channel, callback)

    bus = PostgresEventBus(provider)
    sub = bus.subscribe()
    try:
        for _ in range(100):  # wait until LISTEN has completed and the supervisor is parked
            if sub._conn is not None:
                break
            await asyncio.sleep(0.02)
        assert sub._conn is not None, "the subscription never started listening"

        monkeypatch.setattr(asyncpg.connection.Connection, "remove_listener", slow)
        sub._supervisor.cancel()
        await asyncio.wait_for(entered.wait(), 3.0)
        sub._supervisor.cancel()  # the second cancel, inside the UNLISTEN
        with contextlib.suppress(asyncio.CancelledError):
            await sub._supervisor

        await _assert_all_returned(provider, "after a second cancel landed inside UNLISTEN")
    finally:
        await bus.aclose()


async def test_a_listen_that_fails_on_a_live_connection_does_not_strand_the_pool(
    provider, monkeypatch,
):
    """Every retry used to strand one pooled connection, so after POOL_MAX of
    them the supervisor sat in acquire() forever: the retry count stalls at
    the pool size."""
    attempts: list[int] = []

    async def failing(self, channel, callback, *args, **kwargs):
        attempts.append(1)
        raise RuntimeError("LISTEN failed on a live connection")

    monkeypatch.setattr(asyncpg.connection.Connection, "add_listener", failing)
    bus = PostgresEventBus(provider, reconnect_seconds=0.05)
    sub = bus.subscribe()
    try:
        wanted = POOL_MAX + 3
        for _ in range(300):
            if len(attempts) >= wanted:
                break
            await asyncio.sleep(0.02)
        assert len(attempts) >= wanted, (
            f"only {len(attempts)} LISTEN attempts: the pool ran dry "
            f"({_held(provider)} of {POOL_MAX} connections stranded)"
        )
        assert _held(provider) <= 1  # at most the attempt in flight right now
    finally:
        await sub.aclose()
        await bus.aclose()
    await _assert_all_returned(provider, "after the failing subscription was closed")
