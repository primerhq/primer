"""Unit tests for SessionTickRouter — process-local pub/sub feeding WS subscribers."""

from __future__ import annotations

import asyncio

import pytest

from matrix.session.tick_router import SessionTickRouter, Tick


@pytest.mark.asyncio
async def test_subscribe_yields_published_ticks():
    router = SessionTickRouter()
    sub = router.subscribe("s1")
    router._publish("s1", Tick(seq=5))
    tick = await asyncio.wait_for(anext(sub), timeout=0.1)
    assert tick.seq == 5
    await sub.aclose()


@pytest.mark.asyncio
async def test_multiple_subscribers_each_receive():
    router = SessionTickRouter()
    a = router.subscribe("s1")
    b = router.subscribe("s1")
    router._publish("s1", Tick(seq=1))
    ta = await asyncio.wait_for(anext(a), timeout=0.1)
    tb = await asyncio.wait_for(anext(b), timeout=0.1)
    assert ta.seq == tb.seq == 1
    await a.aclose()
    await b.aclose()


@pytest.mark.asyncio
async def test_aclose_deregisters():
    router = SessionTickRouter()
    sub = router.subscribe("s1")
    await sub.aclose()
    assert "s1" not in router._subs or len(router._subs.get("s1", set())) == 0
