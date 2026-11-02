"""The tap emits every record the messages route serves EXCEPT the compaction marker, over a log the real executor wrote (unrotated, rotated, and compacting).

The long-session e2e journey compares the live tap with ``GET /v1/sessions/{id}/messages`` and found one record per turn (a steer's, by the seqs) the tap never delivered.
This pins the reader half: driven the way the live stream drives it (``read_session_since`` with the byte offset and the seq cursor it got from the previous tick, a tick
after every steer and after every turn), with the default selector (``TapSelector()``, the route's when the client sends none) it delivers each record kind once, none
filtered, but the compaction marker: ``primer/tap/reader.py`` ``_parse_record`` skips it BY DESIGN (an internal history-management record, never activity). A change to the
default selector or to the kinds ``record_to_tap_event`` maps fails here before it fails a journey.
"""

from __future__ import annotations

import shutil

import pytest

from primer.agent.compaction import CompactionStrategy
from primer.api.routers.sessions import _read_workspace_turn_log
from primer.session.dispatch import run_one_session_turn
from primer.session.enqueue import wake_session
from primer.tap.reader import read_batch, read_session_since
from primer.tap.cursor import TapCursor
from primer.tap.selector import TapSelector
from primer.workspace.segments import session_log_for, set_segment_bytes_default
from tests.session.test_dispatch import _make_lease, fake_event_bus, fake_storage_provider  # noqa: F401
from tests.session.test_message_log_order import _ans, _Harness

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git CLI not available on PATH")

TURNS = 8


@pytest.fixture(autouse=True)
def _reset_threshold():
    yield
    set_segment_bytes_default(0)


async def _route_records(h) -> list[tuple[int, str]]:
    items = (await _read_workspace_turn_log(
        workspace=h.workspace, relative_path=f"{getattr(h.workspace, 'state_path', '.state')}/sessions/{h.sid}", log=session_log_for(h.workspace, h.sid),
        limit=500, offset=0, since_seq=None, dedupe_legacy_user_input=True, fallback_created_at="2026-11-02T12:00:00+00:00", workspace_id="ws", session_id=h.sid,
    ))["items"]
    return [(r["seq"], r["kind"]) for r in items if isinstance(r.get("seq"), int) and r["seq"] > 0]   # seq <= 0: a legacy line the route backfills


def _scenario(name: str):
    """``(instructions, scripts, compaction, segment bytes)``: plain answers, or big ones whose second turn compacts (a marker) in a log that rotates."""
    if name == "plain":
        return [f"turn-{n:02d} please" for n in range(TURNS)], [_ans(f"ANSWER-{n:02d} " + "w " * 14_000) for n in range(TURNS)], None, 0
    if name == "rotated":
        return [f"turn-{n:02d} please" for n in range(TURNS)], [_ans(f"ANSWER-{n:02d} " + "w " * 14_000) for n in range(TURNS)], None, 20_000
    # about 50k + 60k tokens of history: the second turn is over the trigger and compacts (the summary is its own model call)
    return ["q" * 200_000, "Q2", "Q3"], [_ans("a" * 240_000), _ans("SUMMARY"), _ans("A2."), _ans("A3.")], CompactionStrategy(tail_turns=1), 100_000


@pytest.mark.parametrize("scenario", ["plain", "rotated", "compacting"])
async def test_the_live_tap_delivers_every_record_the_route_serves_but_the_compaction_marker(tmp_path, fake_storage_provider, fake_event_bus, scenario):
    instructions, scripts, compaction, segment_bytes = _scenario(scenario)
    set_segment_bytes_default(segment_bytes)
    async with _Harness(tmp_path, fake_storage_provider, fake_event_bus, scripts, compaction=compaction) as h:
        tapped: list[tuple[int, str]] = []
        offset = after = 0

        async def tick() -> None:
            nonlocal offset, after
            row = await h.sessions.get(h.sid)
            events, offset = await read_session_since(h.workspace, workspace_id="ws", session=row, after_seq=after, selector=TapSelector(), from_offset=offset)
            for e in events:
                tapped.append((e.seq, e.class_.value))
                after = max(after, e.seq)

        for text in instructions:
            await wake_session(workspace_id=h.workspace.id, session_id=h.sid, instruction=text, human_intent=True, deps=h.wdeps)
            await tick()                                  # the steer's tick
            await run_one_session_turn(_make_lease(h.sid), h.deps)
            row = await h.sessions.get(h.sid)
            await h.sessions.update(row.model_copy(update={"turn_no": row.turn_no + 1}))
            await tick()                                  # the turn's tick

        route = await _route_records(h)
        markers = [r for r in route if r[1] == "compaction_marker"]
        assert bool(markers) == (scenario == "compacting"), route
        if segment_bytes:
            assert len(list((h.workspace.root / h.workspace.template.state_path / "sessions" / h.sid).glob("messages*.jsonl"))) >= (5 if scenario == "rotated" else 2)
        # the one filter: primer/tap/reader.py _parse_record skips the compaction marker by design (history management, not activity)
        assert tapped == [r for r in route if r[1] != "compaction_marker"], "every other record once, in order, with its own kind"
        assert {"user_input", "llm_call", "assistant_token", "done"} <= {k for _, k in route}

        # the multi-session drain (the MCP tap) delivers the same records from a cursor that starts empty
        cursor = TapCursor(seqs={}, known_as_of=(await h.sessions.get(h.sid)).created_at)
        events, _ = await read_batch(fake_storage_provider, h.workspace, workspace_id=h.workspace.id, selector=TapSelector(), cursor=cursor, limit=1000)
        assert [(e.seq, e.class_.value) for e in events if e.session_id == h.sid] == tapped
