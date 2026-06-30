"""Tests for primer.tap.router — WorkspaceTapRouter (sid->wid fan-out).

Exercises the workspace dimension of the tick router: a single bus
subscription consumes ``session:{sid}:tick`` events, resolves each
session's ``workspace_id`` via a cached storage lookup, and fans a
:class:`WorkspaceTick` to every subscriber registered for that
workspace. Uses the real :class:`InMemoryEventBus` and the shared
in-memory storage provider so the contract is exercised end-to-end.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from primer.bus.in_memory import InMemoryEventBus
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.tap.router import WorkspaceTapRouter, WorkspaceTick


pytestmark = pytest.mark.asyncio

_TIMEOUT = 2.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _session(sid: str, wid: str) -> WorkspaceSession:
    return WorkspaceSession(
        id=sid,
        workspace_id=wid,
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.RUNNING,
        created_at=_now(),
    )


async def _publish_tick(bus: InMemoryEventBus, sid: str, seq: int) -> None:
    await bus.publish(f"session:{sid}:tick", {"seq": seq})


async def _next(sub, timeout: float = _TIMEOUT) -> WorkspaceTick:
    return await asyncio.wait_for(sub.__anext__(), timeout=timeout)


async def _seed(storage_provider, session: WorkspaceSession) -> None:
    await storage_provider.get_storage(WorkspaceSession).create(session)


async def _make_router(bus, storage_provider) -> WorkspaceTapRouter:
    router = WorkspaceTapRouter(bus, storage_provider.get_storage(WorkspaceSession))
    await router.start()
    return router


async def test_tick_reaches_subscriber_in_same_workspace(
    fake_storage_provider,
) -> None:
    bus = InMemoryEventBus()
    await bus.initialize()
    await _seed(fake_storage_provider, _session("s1", "W"))
    router = await _make_router(bus, fake_storage_provider)
    try:
        sub = router.subscribe("W")
        try:
            await _publish_tick(bus, "s1", 7)
            tick = await _next(sub)
            assert tick == WorkspaceTick(session_id="s1", seq=7)
        finally:
            await sub.aclose()
    finally:
        await router.aclose()
        await bus.aclose()


async def test_tick_for_other_workspace_does_not_reach_consumer(
    fake_storage_provider,
) -> None:
    bus = InMemoryEventBus()
    await bus.initialize()
    await _seed(fake_storage_provider, _session("s-w", "W"))
    await _seed(fake_storage_provider, _session("s-other", "OTHER"))
    router = await _make_router(bus, fake_storage_provider)
    try:
        sub = router.subscribe("W")
        try:
            # A tick for a session in OTHER must not reach the W consumer.
            await _publish_tick(bus, "s-other", 1)
            # A subsequent tick for W proves routing still works and lets us
            # assert ordering: only the W tick is delivered.
            await _publish_tick(bus, "s-w", 2)
            tick = await _next(sub)
            assert tick == WorkspaceTick(session_id="s-w", seq=2)
        finally:
            await sub.aclose()
    finally:
        await router.aclose()
        await bus.aclose()


async def test_new_session_added_after_start_resolves_via_cache_miss(
    fake_storage_provider,
) -> None:
    bus = InMemoryEventBus()
    await bus.initialize()
    router = await _make_router(bus, fake_storage_provider)
    try:
        sub = router.subscribe("W")
        try:
            # Brand-new session: row added to the store only after start().
            await _seed(fake_storage_provider, _session("s-new", "W"))
            await _publish_tick(bus, "s-new", 3)
            tick = await _next(sub)
            assert tick == WorkspaceTick(session_id="s-new", seq=3)
        finally:
            await sub.aclose()
    finally:
        await router.aclose()
        await bus.aclose()


async def test_unsubscribed_consumer_stops_receiving(
    fake_storage_provider,
) -> None:
    bus = InMemoryEventBus()
    await bus.initialize()
    await _seed(fake_storage_provider, _session("s1", "W"))
    router = await _make_router(bus, fake_storage_provider)
    try:
        sub = router.subscribe("W")
        await _publish_tick(bus, "s1", 1)
        first = await _next(sub)
        assert first == WorkspaceTick(session_id="s1", seq=1)

        await sub.aclose()

        # After close the next() resolves (StopAsyncIteration) rather than
        # delivering further ticks.
        await _publish_tick(bus, "s1", 2)
        with pytest.raises(StopAsyncIteration):
            await _next(sub)
    finally:
        await router.aclose()
        await bus.aclose()


async def test_garbage_key_and_missing_row_do_not_kill_loop(
    fake_storage_provider,
) -> None:
    bus = InMemoryEventBus()
    await bus.initialize()
    await _seed(fake_storage_provider, _session("s-good", "W"))
    router = await _make_router(bus, fake_storage_provider)
    try:
        sub = router.subscribe("W")
        try:
            # 1) A non-tick / garbage key: must be ignored.
            await bus.publish("timer:something", {"foo": "bar"})
            await bus.publish("session::tick", {"seq": 1})  # empty sid
            # 2) A tick whose session row does not exist: log + skip, no crash.
            await _publish_tick(bus, "s-missing", 9)
            # 3) A subsequent valid tick must still route — proving the
            #    consume loop survived all of the above.
            await _publish_tick(bus, "s-good", 5)
            tick = await _next(sub)
            assert tick == WorkspaceTick(session_id="s-good", seq=5)
        finally:
            await sub.aclose()
    finally:
        await router.aclose()
        await bus.aclose()


async def test_aclose_is_idempotent_and_stops_consume_loop(
    fake_storage_provider,
) -> None:
    bus = InMemoryEventBus()
    await bus.initialize()
    router = await _make_router(bus, fake_storage_provider)
    await router.aclose()
    # Second aclose must be a no-op (idempotent), like the bus + sub.
    await router.aclose()
    await bus.aclose()
