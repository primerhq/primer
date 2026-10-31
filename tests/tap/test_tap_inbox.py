"""The SSE stream's inbox: ticks coalesce per session, deltas are bounded, nothing durable is dropped (perf plan Task 13, finding M6).

The stream merged its tick and delta pumps into one unbounded queue, so a slow client's backlog grew in server memory, and every tick of a busy
session made a separate read although one read to the newest seq covers them all. The inbox keeps arrival order, replaces a pending tick of a
session by its newer tick (a tick is only a pointer: the read it triggers goes to the end of the log), and drops the OLDEST delta past its bound
(deltas are ephemeral; the durable log is the record).
"""
from __future__ import annotations

import asyncio

import pytest

from primer.api.routers.tap import MAX_PENDING_DELTAS, _TapInbox
from primer.tap.router import WorkspaceTick

pytestmark = pytest.mark.asyncio


def _tick(sid: str, seq: int) -> WorkspaceTick:
    return WorkspaceTick(session_id=sid, seq=seq)


async def _all(inbox) -> list:
    out = []
    while inbox.pending():
        out.append(await asyncio.wait_for(inbox.get(), 1))
    return out


async def test_order_is_arrival_order_without_coalescing_opportunities():
    inbox = _TapInbox()
    inbox.put_delta({"n": 1})
    inbox.put_tick(_tick("s1", 1))
    inbox.put_delta({"n": 2})
    inbox.put_tick(_tick("s2", 1))
    got = await _all(inbox)
    assert [(tag, item if tag == "delta" else (item.session_id, item.seq)) for tag, item in got] == [
        ("delta", {"n": 1}), ("tick", ("s1", 1)), ("delta", {"n": 2}), ("tick", ("s2", 1)),
    ]


async def test_a_pending_tick_of_a_session_is_replaced_by_its_newer_tick_in_place():
    inbox = _TapInbox()
    inbox.put_tick(_tick("s1", 1))
    inbox.put_delta({"n": 1})
    inbox.put_tick(_tick("s1", 2))
    inbox.put_tick(_tick("s1", 3))
    inbox.put_tick(_tick("s2", 1))
    got = await _all(inbox)
    assert [(tag, item if tag == "delta" else (item.session_id, item.seq)) for tag, item in got] == [
        ("tick", ("s1", 3)), ("delta", {"n": 1}), ("tick", ("s2", 1)),
    ]


async def test_a_tick_arriving_after_the_first_was_taken_is_a_new_entry():
    inbox = _TapInbox()
    inbox.put_tick(_tick("s1", 1))
    assert (await inbox.get())[1].seq == 1
    inbox.put_tick(_tick("s1", 2))
    assert (await inbox.get())[1].seq == 2


async def test_deltas_are_bounded_oldest_first_and_ticks_are_never_dropped():
    inbox = _TapInbox()
    inbox.put_tick(_tick("s1", 1))
    for n in range(MAX_PENDING_DELTAS + 50):
        inbox.put_delta({"n": n})
    inbox.put_tick(_tick("s2", 1))
    got = await _all(inbox)
    deltas = [item["n"] for tag, item in got if tag == "delta"]
    assert len(deltas) == MAX_PENDING_DELTAS and deltas == list(range(50, MAX_PENDING_DELTAS + 50)), "the newest deltas survive"
    assert [item.session_id for tag, item in got if tag == "tick"] == ["s1", "s2"]


async def test_get_waits_for_the_next_item():
    inbox = _TapInbox()
    waiter = asyncio.create_task(inbox.get())
    await asyncio.sleep(0.02)
    assert not waiter.done()
    inbox.put_delta({"n": 1})
    assert await asyncio.wait_for(waiter, 1) == ("delta", {"n": 1})
