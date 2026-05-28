"""Integration tests for the session tick router lifespan wiring.

Verifies that:
1. create_test_app sets app.state.session_tick_router.
2. A bus event with key 'session:{sid}:tick' is delivered to a matching
   subscriber via the in-process SessionTickRouter (forwarder running).
3. A bus event for a different session is not received by an unrelated
   subscriber.
4. _make_lifespan wires session_tick_router on app.state when booting
   with a scheduler (full lifespan smoke test).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI


# ---------------------------------------------------------------------------
# Tests against create_test_app (fast; uses the shared `app` fixture)
# ---------------------------------------------------------------------------


def test_create_test_app_has_session_tick_router(app) -> None:
    """create_test_app must wire app.state.session_tick_router."""
    from matrix.session.tick_router import SessionTickRouter

    router = getattr(app.state, "session_tick_router", None)
    assert router is not None, "app.state.session_tick_router must be set"
    assert isinstance(router, SessionTickRouter)


@pytest.mark.asyncio
async def test_bus_event_reaches_session_router_subscriber(app) -> None:
    """A 'session:{sid}:tick' bus event must reach a subscriber on the router."""
    router = app.state.session_tick_router
    assert router is not None

    # Start the session forwarder so bus events flow to the router.
    fwd = await app.state.start_session_tick_forwarder()
    try:
        sub = router.subscribe("s1")
        await app.state.event_bus.publish("session:s1:tick", {"seq": 7})
        tick = await asyncio.wait_for(sub.__anext__(), timeout=1.0)
        assert tick.seq == 7
        await sub.aclose()
    finally:
        fwd.cancel()
        try:
            await fwd
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_bus_event_for_other_session_not_received(app) -> None:
    """A tick for session 's2' must not reach a subscriber for 's1'."""
    router = app.state.session_tick_router
    fwd = await app.state.start_session_tick_forwarder()
    try:
        sub = router.subscribe("s1")
        await app.state.event_bus.publish("session:s2:tick", {"seq": 99})
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(sub.__anext__(), timeout=0.2)
        await sub.aclose()
    finally:
        fwd.cancel()
        try:
            await fwd
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_non_tick_bus_event_ignored_by_session_router(app) -> None:
    """Events that don't match 'session:*:tick' must be ignored."""
    router = app.state.session_tick_router
    fwd = await app.state.start_session_tick_forwarder()
    try:
        sub = router.subscribe("s1")
        await app.state.event_bus.publish("session:s1:cancel", {"seq": 1})
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(sub.__anext__(), timeout=0.2)
        await sub.aclose()
    finally:
        fwd.cancel()
        try:
            await fwd
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# Lifespan smoke test: _make_lifespan wires session_tick_router
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifespan_wires_session_tick_router(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full lifespan (with in-memory scheduler) must set session_tick_router."""
    monkeypatch.setenv("HOME", str(tmp_path))

    from matrix.api.app import _make_lifespan
    from matrix.api.config import AppConfig
    from matrix.model.scheduler import (
        InMemorySchedulerConfig,
        RuntimeMode,
        SchedulerProviderConfig,
        SchedulerProviderType,
    )
    from matrix.session.tick_router import SessionTickRouter

    cfg = AppConfig(
        runtime_mode=RuntimeMode.API,
        scheduler=SchedulerProviderConfig(
            provider=SchedulerProviderType.IN_MEMORY,
            config=InMemorySchedulerConfig(),
        ),
    )
    _app = FastAPI(lifespan=_make_lifespan(cfg))
    async with _app.router.lifespan_context(_app):
        router = getattr(_app.state, "session_tick_router", None)
        assert router is not None, "lifespan must set app.state.session_tick_router"
        assert isinstance(router, SessionTickRouter)
        # Also verify the forwarder task was created.
        task = getattr(_app.state, "session_tick_forwarder_task", None)
        assert task is not None, "lifespan must set app.state.session_tick_forwarder_task"
