"""W30 and W57b: a stream pays nothing for sessions it does not show."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import primer.api.routers.tap as tap_mod
from primer.tap.cursor import TapCursor
from primer.tap.router import WorkspaceTick
from primer.tap.selector import TapSelector, pinned_session_ids
from tests.tap.sse_fakes import IO, Router, Sess, Store, line, pin_id, seqs_of
from primer.model.storage import FieldRef, Op, Predicate, Value


def _stream(store, selector, io=None, router=None, watch=None, had_cursor=False):
    return tap_mod._stream_tap(
        router=router or Router(), sessions_storage=store, workspace_io=io or IO({}), workspace_id="w",
        selector=selector, cursor=TapCursor(seqs={}, known_as_of=datetime.now(timezone.utc)),
        had_cursor=had_cursor, watch=watch,
    )


async def test_pinned_id_stream_does_no_query_for_foreign_ticks(monkeypatch):
    monkeypatch.setattr(tap_mod, "_KEEPALIVE_INTERVAL_S", 0.3)
    router, store = Router(), Store([Sess("mine")])
    gen = _stream(store, pin_id("mine"), router=router)
    nxt = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0.05)
    before = store.finds
    for i in range(100):
        router.tick.q.put_nowait(WorkspaceTick(session_id="busy-other", seq=i + 1))
    assert await asyncio.wait_for(nxt, 2) == ": keepalive\n\n"
    await gen.aclose()
    assert store.finds - before == 0


async def test_unpinned_stream_still_resolves_an_unknown_ticking_session(monkeypatch):
    monkeypatch.setattr(tap_mod, "_KEEPALIVE_INTERVAL_S", 0.3)
    router, store = Router(), Store([])
    gen = _stream(store, TapSelector(), router=router)
    nxt = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0.05)
    before = store.finds
    router.tick.q.put_nowait(WorkspaceTick(session_id="new", seq=1))
    await asyncio.wait_for(nxt, 2)
    await gen.aclose()
    assert store.finds - before == 1


def _p(name, op, v):
    return Predicate(left=FieldRef(name=name), op=op, right=Value(value=v))


def test_pinned_session_ids_reads_eq_in_and_and():
    assert pinned_session_ids(TapSelector()) is None
    assert pinned_session_ids(pin_id("a")) == {"a"}
    assert pinned_session_ids(TapSelector(sessions=_p("id", Op.IN, ["a", "b"]))) == {"a", "b"}
    both = Predicate(left=_p("id", Op.EQ, "a"), op=Op.AND, right=_p("status", Op.EQ, "x"))
    assert pinned_session_ids(TapSelector(sessions=both)) == {"a"}
    either = Predicate(left=_p("id", Op.EQ, "a"), op=Op.OR, right=_p("status", Op.EQ, "x"))
    assert pinned_session_ids(TapSelector(sessions=either)) is None
    assert pinned_session_ids(TapSelector(sessions=_p("status", Op.EQ, "x"))) is None


class _Watch:
    def __init__(self):
        self.checks = 0

    async def check(self):
        self.checks += 1


async def test_watch_is_checked_only_before_a_tick_that_yields(monkeypatch):
    monkeypatch.setattr(tap_mod, "_KEEPALIVE_INTERVAL_S", 5)
    router, watch = Router(), _Watch()
    store = Store([Sess("s1", last_seq=1)])
    io = IO({"s1": (line(1, "user_input") + line(2, "done")).encode()})
    gen = _stream(store, TapSelector(), io=io, router=router, watch=watch)
    nxt = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0.05)
    for i in range(20):  # foreign ticks: no frames, no check
        router.tick.q.put_nowait(WorkspaceTick(session_id="ghost", seq=i + 1))
    await asyncio.sleep(0.1)
    assert watch.checks == 0
    router.tick.q.put_nowait(WorkspaceTick(session_id="s1", seq=2))
    frames = [await asyncio.wait_for(nxt, 2)]
    await gen.aclose()
    assert seqs_of(frames) == [2]
    assert watch.checks >= 1


async def test_deltas_of_sessions_outside_the_selector_are_not_sent(monkeypatch):
    monkeypatch.setattr(tap_mod, "_KEEPALIVE_INTERVAL_S", 0.3)
    router, store = Router(), Store([Sess("mine")])
    gen = _stream(store, pin_id("mine"), router=router)
    nxt = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0.05)
    router.delta.q.put_nowait({"class": "assistant_delta", "session_id": "other", "text": "x"})
    router.delta.q.put_nowait({"class": "assistant_delta", "session_id": "mine", "text": "y"})
    first = await asyncio.wait_for(nxt, 2)
    await gen.aclose()
    assert '"mine"' in first and "other" not in first


async def test_unscoped_stream_still_sends_every_delta(monkeypatch):
    monkeypatch.setattr(tap_mod, "_KEEPALIVE_INTERVAL_S", 0.3)
    router, store = Router(), Store([])
    gen = _stream(store, TapSelector(), router=router)
    nxt = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0.05)
    router.delta.q.put_nowait({"class": "assistant_delta", "session_id": "any", "text": "x"})
    first = await asyncio.wait_for(nxt, 2)
    await gen.aclose()
    assert '"any"' in first
