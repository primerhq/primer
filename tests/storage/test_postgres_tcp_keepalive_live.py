"""TCP keepalive on REAL pooled connections (live Postgres).

The unit tests drive the init hook with a fake socket. This pins the part a fake
cannot: that ``Connection._transport`` still exists on the installed asyncpg
and yields a socket the options really land on, so an asyncpg upgrade that
moves it fails loudly here instead of silently turning the fix off. It reads
the options back with getsockopt from a connection that came out of a real
``PostgresStorageProvider`` pool, which is also where every LISTEN watcher's
connection comes from.

Gated on the single Postgres test gate (tests/pg_gate.py: PRIMER_TEST_POSTGRES_URL).
"""

from __future__ import annotations

import asyncio
import socket
from urllib.parse import parse_qs, urlparse

import pytest

from primer.model.except_ import ConfigError
from primer.model.provider import PoolConfig, PostgresConfig
from primer.storage.postgres import PostgresStorageProvider
from tests.pg_gate import CANONICAL_ENV, explicit_port, postgres_marks, require_postgres_url

pytestmark = postgres_marks("the live TCP keepalive tests")

_IDLE_OPT = getattr(socket, "TCP_KEEPIDLE", None) or getattr(socket, "TCP_KEEPALIVE", None)


def _config(**pool) -> PostgresConfig:
    p = urlparse(require_postgres_url("the live TCP keepalive tests"))
    if p.scheme not in {"postgres", "postgresql"}:
        raise ConfigError(f"unexpected scheme {p.scheme!r} in {CANONICAL_ENV}")
    schema = parse_qs(p.query).get("schema", ["public"])[0]
    return PostgresConfig(
        hostname=p.hostname or "localhost",
        port=explicit_port(p),
        username=p.username or "postgres",
        password=p.password or "",  # type: ignore[arg-type]
        database=(p.path or "/postgres").lstrip("/") or "postgres",
        db_schema=schema,
        pool=PoolConfig(min_size=1, max_size=4, **pool),
    )


async def _close_bounded(sp) -> None:
    try:
        await asyncio.wait_for(sp.aclose(), 5.0)
    except TimeoutError:
        sp.pool.terminate()
        pytest.fail("pool.close() hung")


def _options(conn) -> dict[str, int]:
    s = conn._transport.get_extra_info("socket")
    return {
        "SO_KEEPALIVE": s.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE),
        "idle": s.getsockopt(socket.IPPROTO_TCP, _IDLE_OPT),
        "interval": s.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL),
        "count": s.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT),
    }


async def test_pooled_connections_carry_the_default_keepalive():
    sp = PostgresStorageProvider(_config())
    await sp.initialize()
    try:
        async with sp.pool.acquire() as conn:
            assert _options(conn) == {"SO_KEEPALIVE": 1, "idle": 60, "interval": 10, "count": 3}
    finally:
        await _close_bounded(sp)


async def test_the_configured_timings_reach_the_socket():
    sp = PostgresStorageProvider(
        _config(tcp_keepalive_idle_seconds=45, tcp_keepalive_interval_seconds=7, tcp_keepalive_count=4),
    )
    await sp.initialize()
    try:
        async with sp.pool.acquire() as conn:
            assert _options(conn) == {"SO_KEEPALIVE": 1, "idle": 45, "interval": 7, "count": 4}
    finally:
        await _close_bounded(sp)


async def test_an_idle_of_zero_leaves_keepalive_off():
    """The off switch really is today's behaviour: the kernel default (no keepalive)."""
    sp = PostgresStorageProvider(_config(tcp_keepalive_idle_seconds=0))
    await sp.initialize()
    try:
        async with sp.pool.acquire() as conn:
            assert _options(conn)["SO_KEEPALIVE"] == 0
    finally:
        await _close_bounded(sp)


async def test_a_connection_opened_after_the_first_one_is_configured_too():
    """The init hook runs per physical connection, so a replacement connection
    (after a drop, or past min_size) must carry keepalive as well."""
    sp = PostgresStorageProvider(_config())
    await sp.initialize()
    try:
        async with sp.pool.acquire() as first, sp.pool.acquire() as second:
            assert first is not second
            assert _options(first)["SO_KEEPALIVE"] == 1
            assert _options(second)["SO_KEEPALIVE"] == 1
    finally:
        await _close_bounded(sp)
