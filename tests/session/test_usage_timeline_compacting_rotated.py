"""On a COMPACTING and ROTATED session the routes' usage and timeline equal what a single-file read gives (seglog PR A, the long-session journey).

The invariants the e2e journey asserts, pinned with the real route functions over a log the real executor wrote, compacting (so it holds markers; a usage fold starts over at a
marker by design: ``primer/session/usage.py``) and rotated into several segments: the row's usage is ``session_usage`` over the segment files JOINED in order, and a turn's
timeline is ``build_turn_timeline_from`` over the joined log, whatever the segments, and does not move when later turns (and later compactions) are added.
"""

from __future__ import annotations

import shutil

import pytest

from primer.agent.compaction import CompactionStrategy
from primer.api.routers.sessions import _read_log_bytes, _session_usage_totals
from primer.api.routers.tap import usage_frame
from primer.session.timeline import build_turn_timeline_from, parse_sources
from primer.session.usage import session_usage
from primer.workspace.segments import ordered, session_log_for, set_segment_bytes_default
from tests.session.test_dispatch import fake_event_bus, fake_storage_provider  # noqa: F401
from tests.session.test_message_log_order import _ans, _Harness

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git CLI not available on PATH")

TURNS = 3


@pytest.fixture(autouse=True)
def _reset_threshold():
    yield
    set_segment_bytes_default(None)


def _joined(h) -> list[str]:
    """The segment files in order, joined: what the single file would hold (the journey reads them through the raw file view)."""
    slot = h.workspace.root / h.workspace.template.state_path / "sessions" / h.sid
    names = [name for _, name in ordered(p.name for p in slot.glob("messages*.jsonl"))]
    return b"".join((slot / n).read_bytes() for n in names).decode("utf-8").splitlines()


async def _facade_lines(h) -> list[str]:
    raw = await _read_log_bytes(h.workspace, f"{getattr(h.workspace, 'state_path', '.state')}/sessions/{h.sid}", workspace_id="ws", session_id=h.sid, log=session_log_for(h.workspace, h.sid))
    return raw.decode("utf-8", errors="replace").splitlines()


async def test_usage_and_timelines_equal_the_single_file_answers_on_a_compacting_rotated_session(tmp_path, fake_storage_provider, fake_event_bus):
    set_segment_bytes_default(100_000)
    scripts = [_ans("a" * 240_000), _ans("SUMMARY"), _ans("A2."), _ans("A3.")]
    async with _Harness(tmp_path, fake_storage_provider, fake_event_bus, scripts, compaction=CompactionStrategy(tail_turns=1)) as h:
        await h.turn("q" * 200_000)
        await h.turn("Q2")
        first_after_two = build_turn_timeline_from(parse_sources(await _facade_lines(h), []), 0)
        await h.turn("Q3")

        lines = _joined(h)
        slot = h.workspace.root / h.workspace.template.state_path / "sessions" / h.sid
        assert len(list(slot.glob("messages*.jsonl"))) >= 2, "rotated"
        assert any('"compaction_marker"' in line for line in lines), "compacting"

        # usage: the row's totals are the fold of the JOINED log (a fold starts over at the marker, so this is NOT turns == inputs)
        row = await h.sessions.get(h.sid)
        got = await _session_usage_totals(row, h.deps.workspace_registry)
        want = usage_frame(session_usage(lines))
        assert got == want, (got, want)
        assert want["turns"] < TURNS, "a compaction folds the turns it replaced away: the count is NOT the number of inputs"

        # timelines: over the facade read (what the route reads) equal those over the joined files, for every turn, and turn 0's did not move
        facade, joined = parse_sources(await _facade_lines(h), []), parse_sources(lines, [])
        for n in range(TURNS):
            assert build_turn_timeline_from(facade, n) == build_turn_timeline_from(joined, n), n
        assert build_turn_timeline_from(facade, 0) == first_after_two, "turn 0's timeline moved when later turns compacted"
