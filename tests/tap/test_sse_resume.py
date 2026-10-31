"""W27, W28, W29: a resumed tap stream catches up on its own, replays only what the client missed, and carries a small cursor."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import primer.api.routers.tap as tap_mod
from primer.tap.cursor import TapCursor
from primer.tap.router import WorkspaceTick
from primer.tap.selector import TapSelector
from tests.tap.sse_fakes import IO, Router, Sess, Store, line, seqs_of

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
OLD, NEW = "2026-01-01T00:00:00+00:00", "2026-06-01T00:00:00+00:00"
MID = datetime(2026, 3, 1, tzinfo=timezone.utc)


def _stream(store, io, cursor, had_cursor, router=None):
    return tap_mod._stream_tap(
        router=router or Router(), sessions_storage=store, workspace_io=io, workspace_id="w",
        selector=TapSelector(), cursor=cursor, had_cursor=had_cursor,
    )


async def _take(gen, n):
    return [await asyncio.wait_for(gen.__anext__(), 2) for _ in range(n)]


async def test_reconnect_catches_up_a_session_that_finished_while_away(monkeypatch):
    monkeypatch.setattr(tap_mod, "_KEEPALIVE_INTERVAL_S", 5)
    log = (line(1, "user_input") + line(2, "tool_call") + line(3, "done")).encode()
    gen = _stream(Store([Sess("s1", last_seq=3)]), IO({"s1": log}), TapCursor(seqs={"s1": 2}, known_as_of=MID), True)
    frames = await _take(gen, 1)
    await gen.aclose()
    assert seqs_of(frames) == [3]


async def test_reconnect_reads_nothing_for_an_idle_session(monkeypatch):
    monkeypatch.setattr(tap_mod, "_KEEPALIVE_INTERVAL_S", 0.1)
    io = IO({"s1": line(1, "done").encode()})
    gen = _stream(Store([Sess("s1", last_seq=1)]), io, TapCursor(seqs={"s1": 1}, known_as_of=MID), True)
    assert (await _take(gen, 1)) == [": keepalive\n\n"]
    await gen.aclose()
    assert io.reads == 0


async def test_reconnect_reads_a_running_session_once(monkeypatch):
    monkeypatch.setattr(tap_mod, "_KEEPALIVE_INTERVAL_S", 0.1)
    io = IO({"s1": line(1, "user_input").encode()})
    gen = _stream(Store([Sess("s1", last_seq=1, turn_status="running")]), io, TapCursor(seqs={"s1": 1}, known_as_of=MID), True)
    await _take(gen, 1)
    await gen.aclose()
    assert io.reads == 1


async def test_the_stream_subscribes_before_it_reads_the_rows(monkeypatch):
    monkeypatch.setattr(tap_mod, "_KEEPALIVE_INTERVAL_S", 0.1)
    order: list[str] = []
    router, store = Router(), Store([])
    subscribe, find = router.subscribe, store.find
    router.subscribe = lambda wid: (order.append("subscribe"), subscribe(wid))[1]

    async def traced_find(pred, page):
        order.append("find")
        return await find(pred, page)

    store.find = traced_find
    gen = _stream(store, IO({}), TapCursor(seqs={}, known_as_of=EPOCH), False, router=router)
    await _take(gen, 1)
    await gen.aclose()
    assert order[:2] == ["subscribe", "find"]


async def test_partial_client_cursor_does_not_replay_other_sessions_history(monkeypatch):
    monkeypatch.setattr(tap_mod, "_KEEPALIVE_INTERVAL_S", 5)
    s2 = "".join(line(i, "tool_call", ts=OLD) for i in range(1, 50)) + line(50, "tool_call", ts=NEW)
    io = IO({"s1": line(1, "done").encode(), "s2": s2.encode()})
    router = Router()
    # a client-built cursor: it names only the session it watches and carries the epoch
    gen = _stream(Store([Sess("s1", 1), Sess("s2", 49)]), io, TapCursor(seqs={"s1": 1}, known_as_of=EPOCH), True, router)
    nxt = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0.05)
    router.tick.q.put_nowait(WorkspaceTick(session_id="s2", seq=50))
    frames = [await asyncio.wait_for(nxt, 2)]
    await gen.aclose()
    assert seqs_of(frames) == [50]


async def test_issued_cursor_replays_unnamed_sessions_only_after_known_as_of(monkeypatch):
    monkeypatch.setattr(tap_mod, "_KEEPALIVE_INTERVAL_S", 5)
    s2 = "".join(line(i, "tool_call", ts=OLD) for i in range(1, 4)) + line(4, "done", ts=NEW)
    active = datetime(2026, 6, 1, tzinfo=timezone.utc)
    io = IO({"s2": s2.encode(), "s3": line(1, "done", ts=OLD).encode()})
    store = Store([Sess("s2", 4, last_activity_at=active), Sess("s3", 1)])
    gen = _stream(store, io, TapCursor(seqs={}, known_as_of=MID), True)
    frames = await _take(gen, 1)
    await gen.aclose()
    assert seqs_of(frames) == [4]  # s3 had no activity since MID: not even read
    assert io.reads == 1


async def test_cursor_stays_small_with_a_thousand_idle_sessions(monkeypatch):
    monkeypatch.setattr(tap_mod, "_KEEPALIVE_INTERVAL_S", 5)
    rows = [Sess(f"sess-{i:012x}", last_seq=1234) for i in range(1000)] + [Sess("live", last_seq=1)]
    io = IO({"live": (line(1, "user_input") + line(2, "done")).encode()})
    router = Router()
    gen = _stream(Store(rows), io, TapCursor(seqs={}, known_as_of=EPOCH), False, router)
    nxt = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0.05)
    router.tick.q.put_nowait(WorkspaceTick(session_id="live", seq=2))
    frame = await asyncio.wait_for(nxt, 2)
    await gen.aclose()
    token = frame.split("id: ", 1)[1].split("\n", 1)[0]
    assert len(token) < 1024
    assert TapCursor.decode(token).seqs == {"live": 2}


async def test_cursor_issued_by_a_fresh_connect_resumes_gap_free(monkeypatch):
    monkeypatch.setattr(tap_mod, "_KEEPALIVE_INTERVAL_S", 5)
    log = (line(1, "user_input") + line(2, "tool_call") + line(3, "done")).encode()
    router = Router()
    gen = _stream(Store([Sess("s1", 1)]), IO({"s1": log}), TapCursor(seqs={}, known_as_of=EPOCH), False, router)
    nxt = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0.05)
    router.tick.q.put_nowait(WorkspaceTick(session_id="s1", seq=2))
    first = await asyncio.wait_for(nxt, 2)
    await gen.aclose()
    token = TapCursor.decode(first.split("id: ", 1)[1].split("\n", 1)[0])
    assert token.seqs == {"s1": 2} and token.known_as_of > EPOCH
    again = _stream(Store([Sess("s1", 3)]), IO({"s1": log}), token, bool(token.seqs))
    frames = await _take(again, 1)
    await again.aclose()
    assert seqs_of(frames) == [3]
