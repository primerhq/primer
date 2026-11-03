"""``scripts/join_session_log.py``: the rollback path of the segmented log (seglog Task 12). It joins a session's segments back into ``messages.jsonl``."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from primer.workspace.segments import ordered, set_segment_bytes_default
from scripts import join_session_log as join
from tests._support.off_golden import open_session

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git CLI not available on PATH")

KEY = "messages.jsonl"


def line(n: int, size: int = 80) -> bytes:
    return (json.dumps({"role": "user", "parts": [{"type": "text", "text": f"{n}:" + "x" * size}]}) + "\n").encode()


@pytest.fixture
async def rotated(tmp_path):
    """A session whose log the real repo rotated; yields (state dir, slot, session id, the joined bytes the single file would hold)."""
    set_segment_bytes_default(300)
    backend, workspace, session = await open_session(tmp_path)
    state = session._state  # noqa: SLF001
    try:
        for n in range(12):
            await state.commit(session.session_id, summary="turn", op="message", appends={KEY: line(n)})
        slot = state.path / "sessions" / session.session_id
        names = [name for _, name in ordered(p.name for p in slot.glob("messages*.jsonl"))]
        assert len(names) > 3
        joined = b"".join((slot / n).read_bytes() for n in names)
        info = json.loads((slot / "session.json").read_text())
        info["status"] = "ended"
        (slot / "session.json").write_text(json.dumps(info))
        yield state.path, slot, session.session_id, joined
    finally:
        set_segment_bytes_default(None)
        await session.aclose()
        await backend.aclose()


def names(slot) -> list[str]:
    return [n for _, n in ordered(p.name for p in slot.glob("messages*.jsonl"))]


def snapshot(slot) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in slot.glob("*") if p.is_file()}


def test_join_restores_the_single_file_bytes(rotated):
    state, slot, sid, joined = rotated
    assert join.main(["--state", str(state), "--session", sid]) == 0
    assert names(slot) == [KEY] and (slot / KEY).read_bytes() == joined
    assert sorted(p.name for p in slot.glob("messages*")) == [KEY], "no marker or temporary file is left behind"
    staged = subprocess.run(["git", "-C", str(state), "diff", "--cached", "--name-only"], check=True, capture_output=True, text=True).stdout
    assert staged.strip() == "", "nothing is staged: the operator commits"
    assert join.main(["--state", str(state), "--session", sid]) == 0, "a joined session is left alone"
    assert (slot / KEY).read_bytes() == joined


def test_dry_run_changes_nothing(rotated, capsys):
    state, slot, sid, _ = rotated
    before = snapshot(slot)
    assert join.main(["--state", str(state), "--all", "--dry-run"]) == 0
    assert snapshot(slot) == before
    assert sid in capsys.readouterr().out


def test_refuses_a_running_session(rotated, capsys):
    state, slot, sid, _ = rotated
    info = json.loads((slot / "session.json").read_text())
    info["status"] = "running"
    (slot / "session.json").write_text(json.dumps(info))
    before = snapshot(slot)
    assert join.main(["--state", str(state), "--session", sid]) == 1
    assert snapshot(slot) == before
    assert "running" in capsys.readouterr().err


def test_refuses_a_log_with_a_gap(rotated, capsys):
    state, slot, sid, _ = rotated
    (slot / "messages.0002.jsonl").unlink()
    before = snapshot(slot)
    assert join.main(["--state", str(state), "--session", sid]) == 1
    assert snapshot(slot) == before, "nothing was joined, nothing removed"
    assert "gap" in capsys.readouterr().err.lower()


def test_a_run_that_died_after_writing_the_joined_file_is_completed_not_doubled(rotated):
    state, slot, sid, joined = rotated
    segments = [n for n in names(slot) if n != KEY]
    # What the script leaves if it dies after the atomic replace and before the unlinks: the joined file, the marker, every segment.
    (slot / KEY).write_bytes(joined)
    (slot / join.MARKER).write_text(json.dumps({"segments": segments, "joined_size": len(joined)}))
    assert join.main(["--state", str(state), "--session", sid]) == 0
    assert names(slot) == [KEY] and (slot / KEY).read_bytes() == joined
    assert not (slot / join.MARKER).exists()


def test_all_joins_every_session_and_skips_the_ones_without_segments(rotated):
    state, slot, sid, joined = rotated
    other = state / "sessions" / "sess-plain"
    other.mkdir()
    (other / KEY).write_bytes(line(1))
    (other / "session.json").write_text(json.dumps({"status": "ended"}))
    assert join.main(["--state", str(state), "--all"]) == 0
    assert (slot / KEY).read_bytes() == joined and (other / KEY).read_bytes() == line(1)
