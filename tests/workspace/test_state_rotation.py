"""Rotating the session log inside ``LocalStateRepo.commit`` (seglog Task 8): the next segment is created and staged in the same commit."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from primer.int.state_repo import LogRewrite
from primer.model.except_ import ConflictError
from primer.workspace.segments import ordered, set_segment_bytes_default
from tests._support.log_golden import SCENARIOS, Env
from tests._support.off_golden import ScriptedLLM, normalise, open_session

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git CLI not available on PATH")

FIXTURES = Path(__file__).parents[1] / "session" / "fixtures" / "message_log_golden"
KEY = "messages.jsonl"


@pytest.fixture(autouse=True)
def _reset_threshold():
    yield
    set_segment_bytes_default(None)


@pytest.fixture
async def world(tmp_path):
    backend, workspace, session = await open_session(tmp_path)
    state = session._state  # noqa: SLF001
    try:
        yield SimpleWorld(workspace, session, state)
    finally:
        await session.aclose()
        await backend.aclose()


class SimpleWorld:
    def __init__(self, workspace, session, state) -> None:
        self.workspace, self.session, self.state = workspace, session, state
        self.sid = session.session_id
        self.slot = state.path / "sessions" / self.sid
        self.first = (self.slot / KEY).read_bytes()      # the session's own first instruction, written when it started

    async def append(self, data: bytes, summary: str = "turn") -> str:
        return await self.state.commit(self.sid, summary=summary, op="message", appends={KEY: data})

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.state.path), *args], check=True, capture_output=True, text=True).stdout

    def names(self) -> list[str]:
        return [name for _, name in ordered(p.name for p in self.slot.glob("messages*.jsonl"))]

    def changed_in_head(self) -> set[str]:
        return {Path(p).name for p in self.git("show", "--name-only", "--format=", "HEAD").split() if "messages" in p}


def line(n: int, size: int = 40) -> bytes:
    return (json.dumps({"role": "user", "parts": [{"type": "text", "text": f"{n}:" + "x" * size}]}) + "\n").encode()


async def test_no_rotation_when_the_threshold_is_zero(world):
    set_segment_bytes_default(0)
    for n in range(6):
        await world.append(line(n, 3000))
    assert world.names() == [KEY]
    assert (world.slot / KEY).read_bytes() == world.first + b"".join(line(n, 3000) for n in range(6))


async def test_rotation_creates_the_next_segment_in_the_same_commit(world):
    set_segment_bytes_default(300)
    await world.append(line(0, 400))                              # 400+ bytes: the active segment is now past the threshold
    with (world.slot / KEY).open("ab") as fh:                     # event rows appended since the last commit (uncommitted)
        fh.write(line(99))
    await world.append(line(1), summary="rotating")
    assert world.names() == [KEY, "messages.0001.jsonl"]
    assert (world.slot / "messages.0001.jsonl").read_bytes() == line(1)
    assert (world.slot / KEY).read_bytes() == world.first + line(0, 400) + line(99)
    assert world.changed_in_head() == {KEY, "messages.0001.jsonl"}, "the end of the old segment and the start of the new one go in one commit"
    assert world.git("status", "--porcelain").strip() == ""


async def test_a_batch_is_never_split_even_when_larger_than_the_threshold(world):
    set_segment_bytes_default(100)
    batch = line(0, 500) + line(1, 500)
    await world.append(batch)
    assert world.names() == [KEY] and (world.slot / KEY).read_bytes() == world.first + batch
    await world.append(batch)
    assert world.names() == [KEY, "messages.0001.jsonl"] and (world.slot / "messages.0001.jsonl").read_bytes() == batch


async def test_old_single_file_session_gets_segment_one_beside_it_and_history_is_not_rewritten(world):
    old = b"".join(line(n, 300) for n in range(20))
    await world.append(old, summary="before the threshold existed")
    blob_before = world.git("rev-parse", f"HEAD:sessions/{world.sid}/{KEY}").strip()
    set_segment_bytes_default(1000)
    await world.append(line(100), summary="first rotation")
    assert world.names() == [KEY, "messages.0001.jsonl"]
    assert world.git("rev-parse", f"HEAD:sessions/{world.sid}/{KEY}").strip() == blob_before, "the old segment was not rewritten"
    commits = world.git("log", "--format=%s", "--", f"sessions/{world.sid}/{KEY}").split("\n")
    assert "before the threshold existed" in commits and "first rotation" not in commits, "only the old commit touched the old file"


@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_concatenation_of_segments_equals_the_single_file_run(name, tmp_path):
    set_segment_bytes_default(2048)
    backend, workspace, session = await open_session(tmp_path)
    try:
        llm = ScriptedLLM()
        llm.session_id = session.session_id
        env = Env(workspace, session, llm, True)
        await SCENARIOS[name](env)
        slot = session._state.path / "sessions" / session.session_id  # noqa: SLF001
        parts = sorted(slot.glob("messages*.jsonl"), key=lambda p: (p.name != KEY, p.name))
        text = b"".join(p.read_bytes() for p in parts).decode()
        want = json.loads((FIXTURES / f"{name}.json").read_text())
        assert normalise(text, session.session_id).splitlines() == want["messages_jsonl"]["lines"]
        log = subprocess.run(
            ["git", "-C", str(session._state.path), "log", "--reverse", "--name-only", "--format=@@%s"],  # noqa: SLF001
            check=True, capture_output=True, text=True,
        ).stdout.split("@@")[1:]
        assert len(log) == len(want["git"])
        for got, golden in zip(log, want["git"]):
            subject, *files = [x for x in got.splitlines() if x.strip()] or [""]
            mapped = sorted({re.sub(r"messages\.\d+\.jsonl", KEY, normalise(f, session.session_id)) for f in files})
            assert mapped == golden["files"], (subject, mapped, golden["files"])
    finally:
        await session.aclose()
        await backend.aclose()


async def test_a_crash_after_creating_the_next_segment_is_repaired_by_the_next_commit(world):
    set_segment_bytes_default(300)
    await world.append(line(0, 400))
    (world.slot / KEY).open("ab").write(line(50))                 # event rows no commit has seen yet
    (world.slot / "messages.0001.jsonl").write_bytes(line(51))    # the rotation created this and died before its commit
    await world.append(line(2), summary="after the crash")
    assert (world.slot / "messages.0001.jsonl").read_bytes() == line(51) + line(2)
    assert world.git("status", "--porcelain").strip() == "", "the old tail and the new segment are both committed now"
    assert (world.slot / KEY).read_bytes() == world.first + line(0, 400) + line(50)


async def test_an_empty_active_segment_is_accepted_by_the_next_commit(world):
    set_segment_bytes_default(300)
    await world.append(line(0, 400))
    (world.slot / "messages.0001.jsonl").write_bytes(b"")
    await world.append(line(1))
    assert (world.slot / "messages.0001.jsonl").read_bytes() == line(1) and world.names() == [KEY, "messages.0001.jsonl"]
    assert world.git("status", "--porcelain").strip() == ""


async def test_whole_file_mode_rotates_with_log_rewrite(world):
    set_segment_bytes_default(150)
    first = world.first + line(0, 200)
    await world.state.commit(world.sid, summary="one", op="message", log_rewrite=LogRewrite(KEY, world.first, line(0, 200)))
    assert (world.slot / KEY).read_bytes() == first
    await world.state.commit(world.sid, summary="two", op="message", log_rewrite=LogRewrite(KEY, first, line(1)))
    assert (world.slot / KEY).read_bytes() == first and (world.slot / "messages.0001.jsonl").read_bytes() == line(1), "only the new lines go to the next segment"
    await world.state.commit(world.sid, summary="three", op="message", log_rewrite=LogRewrite("messages.0001.jsonl", line(1), line(2)))
    assert (world.slot / "messages.0001.jsonl").read_bytes() == line(1) + line(2)
    with pytest.raises(ConflictError):
        await world.state.commit(world.sid, summary="stale", op="message", log_rewrite=LogRewrite(KEY, first, line(3)))
