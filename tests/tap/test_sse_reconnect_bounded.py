"""Review round 1 (I2-2, M-3): the replay after a reconnect is bounded to the stream's lifetime, not repeated on every reconnect."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import primer.api.routers.tap as tap_mod
from primer.tap.cursor import TapCursor
from primer.tap.router import WorkspaceTick
from primer.tap.selector import TapSelector
from tests.tap.sse_fakes import IO, Router, Sess, Store, line

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
SESSIONS = 70  # more than the 64 a token names


class _Clock(datetime):
    """``datetime`` whose ``now`` is the test's clock (the stream stamps ``known_as_of`` with it)."""

    current = T0

    @classmethod
    def now(cls, tz=None):  # noqa: ANN001
        return cls.current


def _at(moment: datetime) -> str:
    return moment.isoformat()


async def _collect(gen, timeout=1.0):
    out, loop = [], asyncio.get_running_loop()
    deadline = loop.time() + timeout
    try:
        while True:
            frame = await asyncio.wait_for(gen.__anext__(), max(0.01, deadline - loop.time()))
            if not frame.startswith(": "):
                out.append(frame)
    except (TimeoutError, StopAsyncIteration):
        return out


def _ids(frames):
    return [(d["session_id"], d["seq"]) for d in (json.loads(f.split("data: ", 1)[1]) for f in frames)]


def _token(frame):
    return frame.split("id: ", 1)[1].split("\n", 1)[0]


def _stream(rows, io, cursor, had_cursor, router=None):
    return tap_mod._stream_tap(
        router=router or Router(), sessions_storage=Store(list(rows.values())), workspace_io=io, workspace_id="w",
        selector=TapSelector(), cursor=cursor, had_cursor=had_cursor,
    )


def _grow(logs, rows, sid, seq, when):
    logs[sid] += line(seq, "user_input", ts=_at(when)).encode()
    rows[sid].last_seq = seq
    rows[sid].last_activity_at = when


async def test_evicted_sessions_are_replayed_once_not_on_every_reconnect(monkeypatch):
    monkeypatch.setattr(tap_mod, "_KEEPALIVE_INTERVAL_S", 0.2)
    monkeypatch.setattr(tap_mod, "datetime", _Clock)
    sids = [f"s{i:03d}" for i in range(SESSIONS)]
    logs = {sid: line(1, "user_input", ts=_at(T0 - timedelta(hours=1))).encode() for sid in sids}
    rows = {sid: Sess(sid, last_seq=1, last_activity_at=T0 - timedelta(hours=1)) for sid in sids}
    io = IO(logs)

    # Stream 1 connects at T0 and sees every session gain seq 2; its tokens name only the newest 64.
    _Clock.current = T0
    router = Router()
    gen = _stream(rows, io, TapCursor(seqs={}, known_as_of=EPOCH), False, router)
    first = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0.05)
    for sid in sids:
        _grow(logs, rows, sid, 2, T0 + timedelta(seconds=10))
        router.tick.q.put_nowait(WorkspaceTick(session_id=sid, seq=2))
    frames = [await asyncio.wait_for(first, 2)] + (await _collect(gen, 1.0))[: SESSIONS - 1]
    await gen.aclose()
    token1 = _token(frames[-1])
    assert "s000" not in TapCursor.decode(token1).seqs

    # While away an evicted (s000) and a named (s069) session gain seq 3. Reconnect 1 replays the evicted ones from T0 (bounded: stream 1's life).
    for sid in ("s000", "s069"):
        _grow(logs, rows, sid, 3, T0 + timedelta(seconds=60))
    _Clock.current = T0 + timedelta(seconds=100)
    gen = _stream(rows, io, TapCursor.decode(token1), True)
    frames1 = await _collect(gen, 1.0)
    await gen.aclose()
    got1 = _ids(frames1)
    assert ("s000", 3) in got1 and ("s069", 3) in got1
    assert ("s069", 2) not in got1
    token2 = _token(frames1[-1])
    assert TapCursor.decode(token2).known_as_of == T0 + timedelta(seconds=100), "the catch-up is done: the horizon moves up"

    # Reconnect 2 (nothing new): nothing is replayed, however many sessions the token left out.
    _Clock.current = T0 + timedelta(seconds=200)
    gen = _stream(rows, io, TapCursor.decode(token2), True)
    assert _ids(await _collect(gen, 1.0)) == []
    await gen.aclose()


async def test_reconnect_with_nothing_to_catch_up_moves_the_horizon_at_once(monkeypatch):
    monkeypatch.setattr(tap_mod, "_KEEPALIVE_INTERVAL_S", 0.2)
    monkeypatch.setattr(tap_mod, "datetime", _Clock)
    _Clock.current = T0 + timedelta(seconds=500)
    logs = {"a": line(1, "user_input", ts=_at(T0)).encode(), "b": line(1, "user_input", ts=_at(T0)).encode()}
    rows = {"a": Sess("a", last_seq=1, last_activity_at=T0), "b": Sess("b", last_seq=1, last_activity_at=T0)}
    router = Router()
    gen = _stream(rows, IO(logs), TapCursor(seqs={"a": 1}, known_as_of=T0), True, router)
    nxt = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0.05)
    _grow(logs, rows, "a", 2, T0 + timedelta(seconds=510))
    router.tick.q.put_nowait(WorkspaceTick(session_id="a", seq=2))
    frame = await asyncio.wait_for(nxt, 2)
    await gen.aclose()
    assert TapCursor.decode(_token(frame)).known_as_of == T0 + timedelta(seconds=500)


async def test_the_token_keeps_the_sessions_most_recently_active_by_their_rows(monkeypatch):
    """M-3: a reconnect seeds its recency from the rows (newest activity last), not from the token's alphabetical key order."""
    monkeypatch.setattr(tap_mod, "_KEEPALIVE_INTERVAL_S", 0.2)
    monkeypatch.setattr(tap_mod, "datetime", _Clock)
    _Clock.current = T0 + timedelta(hours=1)
    sids = [f"s{i:03d}" for i in range(100)]
    rows = {sid: Sess(sid, last_seq=5) for sid in sids}
    for i, sid in enumerate(sids):  # s000 is the NEWEST by activity, s099 the oldest
        rows[sid].last_turn_at = T0 + timedelta(minutes=99 - i)
    logs = {sid: line(5, "done", ts=_at(T0)).encode() for sid in sids}
    cursor = TapCursor(seqs={sid: 5 for sid in sids}, known_as_of=T0)
    router = Router()
    gen = _stream(rows, IO(logs), cursor, True, router)
    nxt = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0.05)
    _grow(logs, rows, "s050", 6, T0 + timedelta(minutes=70))
    router.tick.q.put_nowait(WorkspaceTick(session_id="s050", seq=6))
    frame = await asyncio.wait_for(nxt, 2)
    await gen.aclose()
    kept = set(TapCursor.decode(_token(frame)).seqs)
    assert len(kept) == tap_mod._CURSOR_MAX_SESSIONS
    assert "s050" in kept and "s000" in kept and "s010" in kept, "the newest by row activity survive"
    assert "s099" not in kept and "s090" not in kept, "the oldest by row activity go (alphabetical order would have kept them)"
