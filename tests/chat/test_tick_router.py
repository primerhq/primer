"""Unit tests for ChatTickRouter — process-local pub/sub feeding WS subscribers."""

from __future__ import annotations

import asyncio

import pytest

from matrix.chat.tick_router import ChatTickRouter, Tick


@pytest.mark.asyncio
async def test_single_subscriber_receives_event():
    router = ChatTickRouter()
    sub = router.subscribe("c1")
    router.publish("c1", Tick(seq=5))
    tick = await asyncio.wait_for(sub.__anext__(), timeout=0.5)
    assert tick.seq == 5
    await sub.aclose()


@pytest.mark.asyncio
async def test_two_subscribers_same_chat_both_receive():
    router = ChatTickRouter()
    a = router.subscribe("c1")
    b = router.subscribe("c1")
    router.publish("c1", Tick(seq=7))
    a_tick = await asyncio.wait_for(a.__anext__(), timeout=0.5)
    b_tick = await asyncio.wait_for(b.__anext__(), timeout=0.5)
    assert a_tick.seq == 7
    assert b_tick.seq == 7
    await a.aclose()
    await b.aclose()


@pytest.mark.asyncio
async def test_event_for_other_chat_not_received():
    router = ChatTickRouter()
    sub = router.subscribe("c1")
    router.publish("c2", Tick(seq=99))
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(sub.__anext__(), timeout=0.1)
    await sub.aclose()


@pytest.mark.asyncio
async def test_aclose_deregisters_subscriber():
    router = ChatTickRouter()
    sub = router.subscribe("c1")
    await sub.aclose()
    # Publishing after aclose must not raise; the closed queue is gone.
    router.publish("c1", Tick(seq=11))
    # Internal map should have no subscribers for c1.
    assert "c1" not in router._chat_subs or not router._chat_subs["c1"]


@pytest.mark.asyncio
async def test_publish_to_chat_with_no_subscribers_is_noop():
    router = ChatTickRouter()
    router.publish("c1", Tick(seq=1))
    assert "c1" not in router._chat_subs


@pytest.mark.asyncio
async def test_many_subscribers_no_cross_talk():
    router = ChatTickRouter()
    subs = {cid: router.subscribe(cid) for cid in (f"c{i}" for i in range(10))}
    router.publish("c5", Tick(seq=50))
    t = await asyncio.wait_for(subs["c5"].__anext__(), timeout=0.5)
    assert t.seq == 50
    for cid, s in subs.items():
        if cid == "c5":
            continue
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(s.__anext__(), timeout=0.05)
    for s in subs.values():
        await s.aclose()
