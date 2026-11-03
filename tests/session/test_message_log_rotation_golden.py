"""Seglog Task 11: the REAL writers rotate the log, and everything a reader makes of it is the single-file golden (see ``tests/_support/log_golden.py``).

Every scenario of the message-log golden is driven through the real session and executor with a tiny segment size (512 and 4096 bytes), with the log written whole
and appended. The joined segments must be the golden's bytes, the history, the compaction state, the highest seq and the pending messages must be the golden's, and the
commit history must have the golden's shape (the same commits and subjects; a rotation commit also touches the segment it closed).
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from primer.workspace.message_log import MessageLogCache
from primer.workspace.segments import SessionLog, StateRepoSource, set_segment_bytes_default
from primer.workspace.session import reconstruct_compacted_history
from tests._support.golden_compare import differences
from tests._support.log_golden import SCENARIOS, Env, capture, segment_paths, tool_turn
from tests._support.off_golden import ScriptedLLM, assistant_message, open_session, user_message
from tests._support.segmented_log import Env as ReaderEnv
from tests._support.segmented_log import open_env, run_readers

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git CLI not available on PATH")

FIXTURES = Path(__file__).parent / "fixtures" / "message_log_golden"
SEGMENT_SIZES = (512, 4096)


@pytest.fixture(autouse=True)
def _reset():
    yield
    set_segment_bytes_default(None)


def _golden(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.mark.parametrize("append_only", [False, True])
@pytest.mark.parametrize("segment_bytes", SEGMENT_SIZES)
@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_every_golden_scenario_rotates_and_the_joined_segments_equal_the_golden(name, segment_bytes, append_only, tmp_path):
    got = await capture(name, tmp_path, append_only=append_only, segment_bytes=segment_bytes)
    want = _golden(name)
    segments = got.pop("segments")
    assert sum(segments) == want["messages_jsonl"]["bytes"], "no byte was lost or written twice"
    if want["messages_jsonl"]["bytes"] > 3 * segment_bytes:
        assert len(segments) > 1, "the log was long enough to rotate and did not"
    git = got.pop("git")
    want_git = want.pop("git")
    assert differences(got, want) == []
    # The commit history: the golden's commits and subjects; the files, once a segment name is read as the log, are the golden's too.
    assert [c["subject"] for c in git] == [c["subject"] for c in want_git]
    for have, golden in zip(git, want_git):
        mapped = sorted({re.sub(r"messages\.\d+\.jsonl", "messages.jsonl", f) for f in have["files"]})
        assert mapped == golden["files"], (have["subject"], have["files"])


async def _driven(tmp_path, name, segment_bytes):
    """Run a scenario with rotation on and keep the session open for the readers."""
    set_segment_bytes_default(segment_bytes)
    backend, workspace, session = await open_session(tmp_path / "rotated")
    llm = ScriptedLLM()
    llm.session_id = session.session_id
    await SCENARIOS[name](Env(workspace, session, llm, True))
    return backend, workspace, session


@pytest.mark.parametrize("name", ["plain_turns", "tool_calls", "compaction_marker", "pause_resume", "graph_session", "canvas_records"])
async def test_every_reader_gives_the_same_answer_on_a_log_the_real_writers_rotated(name, tmp_path):
    backend, workspace, session = await _driven(tmp_path, name, 150)
    try:
        rotated = ReaderEnv(backend, workspace, session)
        await rotated.store.create(rotated.row)
        parts = segment_paths(rotated.slot)
        assert len(parts) > 1, "the scenario did not rotate"
        joined = b"".join(p.read_bytes() for p in parts)
        got = await run_readers(rotated)
    finally:
        await session.aclose()
        await backend.aclose()
    set_segment_bytes_default(None)
    single = await open_env(tmp_path / "single")
    try:
        for stale in single.slot.glob("messages*.jsonl"):
            stale.unlink()
        (single.slot / "messages.jsonl").write_bytes(joined.replace(rotated.sid.encode(), single.sid.encode()))
        want = await run_readers(single)
    finally:
        await single.close()
    assert {r: v for r, v in got.items() if v != want[r]} == {}


async def test_a_rotation_between_two_reads_of_a_warm_cache(tmp_path):
    """Review Focus 2: the cache is warm, the log rotates (and compacts), the next view equals a fresh parse of the joined segments."""
    set_segment_bytes_default(512)
    backend, workspace, session = await open_session(tmp_path)
    try:
        llm = ScriptedLLM()
        llm.session_id = session.session_id
        env = Env(workspace, session, llm, True)
        await env.ex._persist_turn([user_message("start")])
        warm = await session.log_view()
        seen = {len(segment_paths(env.messages_path.parent))}
        for i in range(1, 14):
            await env.ex._persist_turn(tool_turn(i))
            if i == 6:
                summary = assistant_message("[earlier conversation compacted]\n\nSUMMARY of the first turns")
                await env.ex._replace_compacted_head([summary], summary_message=summary, tokens_before=900, tokens_after=100, trigger_tokens=800)
            view = await session.log_view()
            joined = b"".join(p.read_bytes() for p in segment_paths(env.messages_path.parent)).decode()
            want = reconstruct_compacted_history(joined.splitlines())
            assert [m.model_dump(mode="json") for m in view.history] == [m.model_dump(mode="json") for m in want], i
            fresh = await MessageLogCache(verify=False).view_log(SessionLog(StateRepoSource(session._state), f"sessions/{session.session_id}"))  # noqa: SLF001
            assert (view.history, view.compaction, view.max_seq) == (fresh.history, fresh.compaction, fresh.max_seq), i
            seen.add(len(segment_paths(env.messages_path.parent)))
        assert max(seen) >= 3, "the log rotated more than once while the cache stayed warm"
        assert warm.size < (await session.log_view()).size
    finally:
        await session.aclose()
        await backend.aclose()


@pytest.mark.parametrize("append_only", [False, True])
async def test_event_rows_after_a_rotation_land_in_the_active_segment_and_closed_segments_never_change(append_only, tmp_path):
    """Turns (committed) and event rows (not) interleaved across many rotations: each row is the last line of the log when written, and a closed segment keeps its bytes."""
    import subprocess

    from tests._support.log_golden import record
    from primer.model.workspace_session import SessionMessageKind

    set_segment_bytes_default(400)
    backend, workspace, session = await open_session(tmp_path)
    try:
        llm = ScriptedLLM()
        llm.session_id = session.session_id
        env = Env(workspace, session, llm, append_only)
        slot = env.messages_path.parent
        closed: dict[str, bytes] = {}
        for i in range(1, 25):
            await env.ex._persist_turn(tool_turn(i))
            row = record(i, SessionMessageKind.CANVAS_EVENT, {"event": "click", "i": i})
            await env.event_line(row)
            parts = segment_paths(slot)
            assert parts[-1].read_bytes().endswith((row.model_dump_json() + "\n").encode()), f"row {i} is not at the end of the active segment"
            for part in parts[:-1]:
                closed.setdefault(part.name, part.read_bytes())
                assert part.read_bytes() == closed[part.name], f"closed segment {part.name} changed"
        assert len(segment_paths(slot)) >= 4
        await env.ex._persist_turn([assistant_message("last")])       # one more commit stages the rows written since the previous one
        status = subprocess.run(["git", "-C", str(session._state.path), "status", "--porcelain"], check=True, capture_output=True, text=True).stdout  # noqa: SLF001
        assert status.strip() == "", status
    finally:
        await session.aclose()
        await backend.aclose()
