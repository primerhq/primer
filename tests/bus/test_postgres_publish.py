"""Unit tests for PostgresEventBus.publish, including the NOTIFY payload cap.

Postgres NOTIFY caps payloads at 8000 bytes and otherwise raises an opaque
``payload string too long``. The bus guards slightly under that
(``MAX_NOTIFY_PAYLOAD_BYTES``) and raises a clear domain error BEFORE touching
a connection (BE10b). These tests drive that guard, and the happy path, with a
fake pool -- no real Postgres server.
"""

from __future__ import annotations

import json

import pytest

from primer.bus.postgres import (
    MAX_NOTIFY_PAYLOAD_BYTES,
    PostgresEventBus,
    YIELD_EVENTS_CHANNEL,
)
from primer.model.except_ import ProviderError

pytestmark = pytest.mark.asyncio


class _RecordingConn:
    """Records every ``execute`` (the NOTIFY) issued against it."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, *args) -> None:  # noqa: ANN002
        self.executed.append((sql, args))


class _AcquireCtx:
    """Async context manager mirroring asyncpg ``pool.acquire()``."""

    def __init__(self, pool: "_PublishPool") -> None:
        self._pool = pool

    async def __aenter__(self) -> _RecordingConn:
        conn = _RecordingConn()
        self._pool.acquired.append(conn)
        return conn

    async def __aexit__(self, *exc) -> bool:  # noqa: ANN002
        return False


class _PublishPool:
    """Records each acquire so a test can assert the pool was (not) touched."""

    def __init__(self) -> None:
        self.acquired: list[_RecordingConn] = []

    def acquire(self) -> _AcquireCtx:
        return _AcquireCtx(self)


class _PublishStorage:
    def __init__(self, pool: _PublishPool) -> None:
        self.pool = pool


async def test_publish_rejects_oversized_payload() -> None:
    """A NOTIFY payload over the safe byte cap raises a clear ProviderError
    before any connection is acquired -- not an opaque asyncpg error."""
    pool = _PublishPool()
    bus = PostgresEventBus(_PublishStorage(pool))
    await bus.initialize()
    try:
        big = {"blob": "x" * (MAX_NOTIFY_PAYLOAD_BYTES + 1000)}
        with pytest.raises(ProviderError) as ei:
            await bus.publish("ask_user:1", big)
        assert ei.value.code == "notify_payload_too_long"
        assert str(MAX_NOTIFY_PAYLOAD_BYTES) in ei.value.message
        # The guard fired BEFORE touching the pool: no connection acquired,
        # so no opaque "payload string too long" ever reached asyncpg.
        assert pool.acquired == []
    finally:
        await bus.aclose()


async def test_publish_small_payload_notifies() -> None:
    """A small routing-key payload publishes normally: the guard does not
    false-positive and a NOTIFY is issued on an acquired connection."""
    pool = _PublishPool()
    bus = PostgresEventBus(_PublishStorage(pool))
    await bus.initialize()
    try:
        await bus.publish("ask_user:1", {"session_id": "s-1"})
        assert len(pool.acquired) == 1
        sql, args = pool.acquired[0].executed[0]
        assert "pg_notify" in sql
        assert YIELD_EVENTS_CHANNEL in sql
        body = json.loads(args[0])
        assert body == {
            "event_key": "ask_user:1",
            "payload": {"session_id": "s-1"},
        }
    finally:
        await bus.aclose()


async def test_publish_at_threshold_is_allowed() -> None:
    """A payload whose encoded body is exactly at the cap is allowed; one byte
    over is rejected. Pins the boundary so the guard neither over- nor
    under-shoots."""
    pool = _PublishPool()
    bus = PostgresEventBus(_PublishStorage(pool))
    await bus.initialize()
    try:
        # Craft a body whose UTF-8 length lands exactly on the cap. The wrapper
        # JSON around the "blob" value has a fixed byte overhead; size the value
        # so the whole body == MAX_NOTIFY_PAYLOAD_BYTES.
        base = json.dumps({"event_key": "k", "payload": {"blob": ""}})
        overhead = len(base.encode("utf-8"))
        fill = MAX_NOTIFY_PAYLOAD_BYTES - overhead
        await bus.publish("k", {"blob": "x" * fill})
        assert len(pool.acquired) == 1

        # One byte over the cap is rejected.
        with pytest.raises(ProviderError):
            await bus.publish("k", {"blob": "x" * (fill + 1)})
    finally:
        await bus.aclose()
