"""End-to-end: a bus event with key 'chat:{cid}:tick' is delivered
to a matching subscriber via the in-process ChatTickRouter wired
into app.state."""

from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_bus_event_reaches_router_subscriber(app):
    router = app.state.chat_tick_router
    assert router is not None
    sub = router.subscribe("c1")
    await app.state.event_bus.publish("chat:c1:tick", {"seq": 42})
    tick = await asyncio.wait_for(sub.__anext__(), timeout=1.0)
    assert tick.seq == 42
    await sub.aclose()


@pytest.mark.asyncio
async def test_bus_event_for_other_chat_not_received(app):
    router = app.state.chat_tick_router
    sub = router.subscribe("c1")
    await app.state.event_bus.publish("chat:c2:tick", {"seq": 99})
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(sub.__anext__(), timeout=0.2)
    await sub.aclose()


@pytest.mark.asyncio
async def test_bus_event_with_non_tick_key_ignored(app):
    router = app.state.chat_tick_router
    sub = router.subscribe("c1")
    await app.state.event_bus.publish("chat-claimable", {"chat_id": "c1"})
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(sub.__anext__(), timeout=0.2)
    await sub.aclose()
