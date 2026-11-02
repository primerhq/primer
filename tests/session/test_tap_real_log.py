"""The tap emits EVERY record the messages route serves, over a log the real executor wrote (unrotated and rotated).

The long-session e2e journey compares the live tap with ``GET /v1/sessions/{id}/messages`` and found one record per turn (a steer's, by the seqs) the tap never delivered.
This pins the reader half: driven the way the live stream drives it (``read_session_since`` with the byte offset and the seq cursor it got from the previous tick, a tick
after every steer and after every turn), with the default selector (``TapSelector()``, the route's when the client sends none) it delivers each record kind once, none
filtered. A change to the default selector or to the kinds ``record_to_tap_event`` maps fails here before it fails a journey.
"""

from __future__ import annotations

import shutil

import pytest

from primer.api.routers.sessions import _read_workspace_turn_log
from primer.session.dispatch import run_one_session_turn
from primer.session.enqueue import wake_session
from primer.tap.reader import read_batch, read_session_since
from primer.tap.cursor import TapCursor
from primer.tap.selector import TapSelector
from primer.workspace.segments import session_log_for, set_segment_bytes_default
from tests._support.session_records import record_seqs
from tests.session.test_dispatch import _make_lease, fake_event_bus, fake_storage_provider  # noqa: F401
from tests.session.test_message_log_order import _ans, _Harness

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git CLI not available on PATH")

TURNS = 8


@pytest.fixture(autouse=True)
def _reset_threshold():
    yield
    set_segment_bytes_default(0)


async def _route_records(h) -> dict[int, str]:
    items = (await _read_workspace_turn_log(
        workspace=h.workspace, relative_path=f"{getattr(h.workspace, 'state_path', '.state')}/sessions/{h.sid}", log=session_log_for(h.workspace, h.sid),
        limit=500, offset=0, since_seq=None, dedupe_legacy_user_input=True, fallback_created_at="2026-11-02T12:00:00+00:00", workspace_id="ws", session_id=h.sid,
    ))["items"]
    return {r["seq"]: r["kind"] for r in items if isinstance(r.get("seq"), int) and r["seq"] > 0}   # seq <= 0: a legacy line the route backfills


@pytest.mark.parametrize("segment_bytes", [0, 20_000])
async def test_the_live_tap_delivers_every_record_the_route_serves(tmp_path, fake_storage_provider, fake_event_bus, segment_bytes):
    set_segment_bytes_default(segment_bytes)
    scripts = [_ans(f"ANSWER-{n:02d} " + "w " * 14_000) for n in range(TURNS)]
    async with _Harness(tmp_path, fake_storage_provider, fake_event_bus, scripts) as h:
        tapped: list[tuple[int, str]] = []
        offset = after = 0

        async def tick() -> None:
            nonlocal offset, after
            row = await h.sessions.get(h.sid)
            events, offset = await read_session_since(h.workspace, workspace_id="ws", session=row, after_seq=after, selector=TapSelector(), from_offset=offset)
            for e in events:
                tapped.append((e.seq, e.class_.value))
                after = max(after, e.seq)

        for n in range(TURNS):
            await wake_session(workspace_id=h.workspace.id, session_id=h.sid, instruction=f"turn-{n:02d} please", human_intent=True, deps=h.wdeps)
            await tick()                                  # the steer's tick
            await run_one_session_turn(_make_lease(h.sid), h.deps)
            row = await h.sessions.get(h.sid)
            await h.sessions.update(row.model_copy(update={"turn_no": row.turn_no + 1}))
            await tick()                                  # the turn's tick

        route = await _route_records(h)
        if segment_bytes:
            assert len(list((h.workspace.root / h.workspace.template.state_path / "sessions" / h.sid).glob("messages*.jsonl"))) >= 5
        assert [s for s, _ in tapped] == sorted(route), "every record once, in order"
        assert dict(tapped) == route, "with the same kind each: nothing the route serves is filtered out of the default tap"
        assert {"user_input", "llm_call", "assistant_token", "done"} <= set(route.values())

        # the multi-session drain (the MCP tap) delivers the same set from a cursor that starts empty
        cursor = TapCursor(seqs={}, known_as_of=(await h.sessions.get(h.sid)).created_at)
        events, _ = await read_batch(fake_storage_provider, h.workspace, workspace_id=h.workspace.id, selector=TapSelector(), cursor=cursor, limit=1000)
        assert {e.seq: e.class_.value for e in events if e.session_id == h.sid} == route
        assert record_seqs([{"seq": s} for s in route]) == sorted(route)
