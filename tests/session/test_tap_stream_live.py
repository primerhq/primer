"""The live tap STREAM (``_stream_tap``) against a real executor-written log, driven the way the server drives it (seglog PR A, the long-session journey).

The stream subscribes BEFORE the session exists (as the journey's does), then a session is created, steered from the API side (``wake_session``, with the tick it publishes
on the bus), run by the dispatch (its own ticks), steered again, and so on, all in one event loop so the stream reads in between. Every record the messages route serves
must come out of the stream once. A steer's ``user_input`` that does not is the e2e's gap, reproduced.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from datetime import datetime, timezone

import pytest

import primer.api.routers.tap as tap_mod
from primer.api.routers.sessions import _read_workspace_turn_log
from primer.session.dispatch import run_one_session_turn
from primer.session.enqueue import wake_session
from primer.tap.cursor import TapCursor
from primer.tap.router import WorkspaceTapRouter
from primer.tap.selector import TapSelector
from primer.workspace.segments import session_log_for, set_segment_bytes_default
from tests.session.test_dispatch import _make_lease, fake_event_bus, fake_storage_provider  # noqa: F401
from tests.session.test_message_log_order import _ans, _Harness

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git CLI not available on PATH")

TURNS = 6


@pytest.fixture(autouse=True)
def _reset_threshold():
    yield
    set_segment_bytes_default(0)


async def _route(h) -> dict[int, str]:
    items = (await _read_workspace_turn_log(
        workspace=h.workspace, relative_path=f"{getattr(h.workspace, 'state_path', '.state')}/sessions/{h.sid}", log=session_log_for(h.workspace, h.sid),
        limit=500, offset=0, since_seq=None, dedupe_legacy_user_input=True, fallback_created_at="2026-11-02T12:00:00+00:00", workspace_id="ws", session_id=h.sid,
    ))["items"]
    return {r["seq"]: r["kind"] for r in items if isinstance(r.get("seq"), int) and r["seq"] > 0}


@pytest.mark.parametrize("segment_bytes", [0, 20_000])
async def test_every_record_of_a_steered_session_comes_out_of_the_live_stream(tmp_path, fake_storage_provider, fake_event_bus, segment_bytes):
    set_segment_bytes_default(segment_bytes)
    scripts = [_ans(f"ANSWER-{n:02d} " + "w " * 14_000) for n in range(TURNS)]
    async with _Harness(tmp_path, fake_storage_provider, fake_event_bus, scripts) as h:
        row = await h.sessions.get(h.sid)
        await h.sessions.delete(h.sid)                        # the stream connects before the session exists
        router = WorkspaceTapRouter(fake_event_bus, h.sessions)
        await router.start()
        frames: list[dict] = []
        stream = tap_mod._stream_tap(
            router=router, sessions_storage=h.sessions, workspace_io=h.workspace, workspace_id=row.workspace_id, selector=TapSelector(),
            cursor=TapCursor(seqs={}, known_as_of=datetime.now(timezone.utc)), had_cursor=False,
        )

        async def follow() -> None:
            async for chunk in stream:
                for line in chunk.splitlines():
                    if line.startswith("data: "):
                        frames.append(json.loads(line[6:]))

        follower = asyncio.create_task(follow())
        try:
            await asyncio.sleep(0.2)                           # the stream is subscribed and idle
            await h.sessions.create(row)
            for n in range(TURNS):
                await wake_session(workspace_id=row.workspace_id, session_id=h.sid, instruction=f"turn-{n:02d} please", human_intent=True, deps=h.wdeps)
                await run_one_session_turn(_make_lease(h.sid), h.deps)
                current = await h.sessions.get(h.sid)
                await h.sessions.update(current.model_copy(update={"turn_no": current.turn_no + 1}))
            route = await _route(h)
            for _ in range(100):
                if set(route) <= {f["seq"] for f in frames if f.get("session_id") == h.sid and isinstance(f.get("seq"), int)}:
                    break
                await asyncio.sleep(0.05)
            got = {f["seq"]: f["class"] for f in frames if f.get("session_id") == h.sid and isinstance(f.get("seq"), int)}
            assert got == route, f"missing from the stream (seq: kind) { {s: route[s] for s in sorted(set(route) - set(got))} }; extra {sorted(set(got) - set(route))}"
        finally:
            follower.cancel()
            await asyncio.gather(follower, return_exceptions=True)
            await stream.aclose()
            await router.aclose()
