"""W57e: a frame after a binding switch carries the session's new agent."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import primer.api.routers.tap as tap_mod
from primer.tap.cursor import TapCursor
from primer.tap.router import WorkspaceTick
from primer.tap.selector import TapSelector
from tests.tap.sse_fakes import IO, Router, Sess, Store, line


async def test_frames_after_an_agent_marker_carry_the_new_agent(monkeypatch):
    monkeypatch.setattr(tap_mod, "_KEEPALIVE_INTERVAL_S", 5)
    old = Sess("s1", last_seq=0)
    old.binding = SimpleNamespace(agent_id="agent-old")
    store = Store([old])
    log = line(1, "agent_marker") + line(2, "user_input")
    router = Router()
    gen = tap_mod._stream_tap(
        router=router, sessions_storage=store, workspace_io=IO({"s1": log.encode()}), workspace_id="w",
        selector=TapSelector(), cursor=TapCursor(seqs={}, known_as_of=datetime.now(timezone.utc)), had_cursor=False,
    )
    nxt = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0.05)
    switched = Sess("s1", last_seq=2)
    switched.binding = SimpleNamespace(agent_id="agent-new")
    store.rows["s1"] = switched
    router.tick.q.put_nowait(WorkspaceTick(session_id="s1", seq=2))
    frames = [await asyncio.wait_for(nxt, 2), await asyncio.wait_for(gen.__anext__(), 2)]
    await gen.aclose()
    assert [json.loads(f.split("data: ", 1)[1])["agent_id"] for f in frames] == ["agent-new", "agent-new"]
