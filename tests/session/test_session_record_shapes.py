"""The shapes of ``GET /v1/sessions/{id}/messages`` items, over a log the REAL executor wrote (the long-session e2e journey parses them; it must not guess).

Real ``wake_session`` + ``run_one_session_turn`` + ``WorkspaceAgentExecutor`` over a local workspace (the harness of ``test_message_log_order``), read back through the
route's own reader, segmented or not. Also pins what the journey reads next to it: the usage fold counts one turn and one model call per answer, and a finished turn's
timeline does not change when later turns are added.
"""

from __future__ import annotations

import shutil

import pytest

from primer.api.routers.sessions import _read_log_bytes, _read_workspace_turn_log
from primer.session.timeline import build_turn_timeline_from, parse_sources
from primer.session.usage import session_usage
from primer.workspace.segments import session_log_for, set_segment_bytes_default
from tests._support.session_records import assistant_answers, record_seqs, user_inputs
from tests.session.test_dispatch import fake_event_bus, fake_storage_provider  # noqa: F401
from tests.session.test_message_log_order import _ans, _Harness

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git CLI not available on PATH")

TURNS = 4


@pytest.fixture(autouse=True)
def _reset_threshold():
    yield
    set_segment_bytes_default(0)


async def _route_items(h) -> list[dict]:
    return (await _read_workspace_turn_log(
        workspace=h.workspace, relative_path=f"{getattr(h.workspace, 'state_path', '.state')}/sessions/{h.sid}", log=session_log_for(h.workspace, h.sid), limit=500, offset=0, since_seq=None,
        dedupe_legacy_user_input=True, fallback_created_at="2026-11-02T12:00:00+00:00", workspace_id="ws", session_id=h.sid,
    ))["items"]


@pytest.mark.parametrize("segment_bytes", [0, 600])
async def test_the_steers_and_the_answers_are_each_found_once(tmp_path, fake_storage_provider, fake_event_bus, segment_bytes):
    set_segment_bytes_default(segment_bytes)
    scripts = [_ans(f"ANSWER-{n:02d} " + "word " * 200) for n in range(TURNS)]
    async with _Harness(tmp_path, fake_storage_provider, fake_event_bus, scripts) as h:
        for n in range(TURNS):
            await h.turn(f"turn-{n:02d} please")
        items = await _route_items(h)
        segments = list((h.workspace.root / h.workspace.template.state_path / "sessions" / h.sid).glob("messages*.jsonl"))
        assert (len(segments) >= 3) if segment_bytes else (len(segments) == 1), segments
        assert [r for r in items if "kind" not in r and r.get("role") not in ("user", "assistant", "tool")] == []
        seqs = record_seqs(items)
        assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))
        steers = [t for t in user_inputs(items) if t.startswith("turn-")]
        assert steers == [f"turn-{n:02d} please" for n in range(TURNS)], "each steer exactly once, as its user_input record"
        answers = assistant_answers(items)
        for n in range(TURNS):
            assert sum(f"ANSWER-{n:02d}" in text for text in answers) == 1, (n, [a[:12] for a in answers])


async def test_usage_counts_one_turn_and_one_model_call_per_answer_and_a_finished_timeline_is_stable(tmp_path, fake_storage_provider, fake_event_bus):
    scripts = [_ans(f"ANSWER-{n:02d}") for n in range(TURNS)]
    async with _Harness(tmp_path, fake_storage_provider, fake_event_bus, scripts) as h:
        await h.turn("turn-00 please")
        await h.turn("turn-01 please")
        first = build_turn_timeline_from(parse_sources((await _log_lines(h)), []), 0)
        for n in range(2, TURNS):
            await h.turn(f"turn-{n:02d} please")
        lines = await _log_lines(h)
        usage = session_usage(lines)
        assert (usage.turns, usage.model_calls) == (TURNS, TURNS)
        assert build_turn_timeline_from(parse_sources(lines, []), 0) == first, "turn 0's timeline changed when later turns were added"


async def _log_lines(h) -> list[str]:
    raw = await _read_log_bytes(h.workspace, f"{getattr(h.workspace, 'state_path', '.state')}/sessions/{h.sid}", workspace_id="ws", session_id=h.sid, log=session_log_for(h.workspace, h.sid))
    return raw.decode("utf-8", errors="replace").splitlines()
