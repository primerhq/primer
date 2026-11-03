"""The sandbox state repo rotates the session log by whole-file writes (seglog Task 9): no runtime protocol change, the closed segment is not sent again."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime, timezone

import pytest

from primer.int.sandbox import FileStat
from primer.int.state_repo import LogRewrite
from primer.model.except_ import ConflictError
from primer.workspace.sandbox.state import SandboxStateRepo
from primer.workspace.segments import ordered, read_active_segment, set_segment_bytes_default
from tests._support.log_golden import SCENARIOS, Env
from tests._support.off_golden import ScriptedLLM, normalise, open_session
from tests.workspace.test_state_rotation import FIXTURES

KEY = "messages.jsonl"
SID = "sess-sandbox"
STATE = "/workspace/.state"


class Runtime:
    """The runtime ops the sandbox state repo uses, over a dict; ``state_commit`` has exactly the signature of the shipped runtime."""

    protocol_version = "1.1"

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.sent: list[dict[str, int]] = []

    async def state_commit(self, *, files: dict[str, bytes], deletes: list[str], message: str, allow_empty: bool = False) -> str:
        self.sent.append({p: len(c) for p, c in files.items()})
        self.files.update(files)
        return "a" * 40

    async def state_history(self, **_kw):
        return []

    async def state_read(self, paths: list[str]):
        return {p: self.files.get(p) for p in paths}

    async def stat(self, path: str):
        rel = path.removeprefix(STATE + "/")
        return type("S", (), {"kind": "dir"})() if any(f.startswith(rel + "/") for f in self.files) else None

    async def list_dir(self, path: str):
        rel = path.removeprefix(STATE + "/") + "/"
        now = datetime.now(timezone.utc)
        return [FileStat(path=f"{path}/{f[len(rel):]}", kind="file", size_bytes=len(c), mode=0o644, modified_at=now) for f, c in self.files.items() if f.startswith(rel)]


@pytest.fixture(autouse=True)
def _reset_threshold():
    yield
    set_segment_bytes_default(0)


def make():
    runtime = Runtime()
    repo = SandboxStateRepo(runtime, state_path=STATE, workspace_id="w")
    repo._agent_by_session[SID] = "agent"  # noqa: SLF001
    return runtime, repo


def line(n: int, size: int = 60) -> bytes:
    return (json.dumps({"role": "user", "parts": [{"type": "text", "text": f"{n}:" + "x" * size}]}) + "\n").encode()


async def add(repo, data: bytes) -> str:
    """What a whole-file writer does: read the active segment under the lock, hand it back with the new lines."""
    name, existing = await read_active_segment(repo, SID)
    return await repo.commit(SID, summary="turn", op="message", log_rewrite=LogRewrite(name, existing, data))


async def test_sandbox_commit_sends_only_the_active_segment():
    set_segment_bytes_default(400)
    runtime, repo = make()
    for n in range(60):
        await add(repo, line(n))
    total = sum(len(v) for v in runtime.files.values())
    assert total > 5000 and len(runtime.files) > 5
    assert max(sum(c.values()) for c in runtime.sent) <= 400 + len(line(0)) * 2, "a commit sends at most one active segment, whatever the log size"


async def test_sandbox_rotation_writes_new_lines_to_the_next_segment():
    set_segment_bytes_default(150)
    runtime, repo = make()
    first = line(0, 200)
    await add(repo, first)
    assert list(runtime.sent[-1]) == [f"sessions/{SID}/{KEY}"]
    await add(repo, line(1))
    assert runtime.sent[-1] == {f"sessions/{SID}/messages.0001.jsonl": len(line(1))}, "the closed segment is not sent again"
    assert runtime.files[f"sessions/{SID}/{KEY}"] == first
    await add(repo, line(2))
    assert runtime.files[f"sessions/{SID}/messages.0001.jsonl"] == line(1) + line(2)
    with pytest.raises(ConflictError):
        await repo.commit(SID, summary="stale", op="message", log_rewrite=LogRewrite(KEY, first, line(3)))
    with pytest.raises(ConflictError):
        await repo.commit(SID, summary="legacy", op="message", files={KEY: "x"})


async def test_threshold_zero_never_rotates():
    runtime, repo = make()
    for n in range(20):
        await add(repo, line(n, 500))
    assert list(runtime.files) == [f"sessions/{SID}/{KEY}"]


async def test_old_runtime_image_is_enough():
    runtime, repo = make()
    await add(repo, line(0))        # the double accepts only files, deletes, message, allow_empty: no new keyword reached it


pytestmark_git = pytest.mark.skipif(shutil.which("git") is None, reason="git CLI not available on PATH")


@pytestmark_git
@pytest.mark.parametrize("name", sorted(set(SCENARIOS) - {"graph_session", "canvas_records"}))   # the event-row scenarios wait for Task 10
async def test_whole_file_writers_rotate_and_concat_equals_the_golden(name, tmp_path):
    """The executor's and the session's whole-file branches (the flag off, as on a sandbox) write through log_rewrite and rotate."""
    set_segment_bytes_default(2048)
    backend, workspace, session = await open_session(tmp_path)
    try:
        llm = ScriptedLLM()
        llm.session_id = session.session_id
        await SCENARIOS[name](Env(workspace, session, llm, False))
        slot = session._state.path / "sessions" / session.session_id  # noqa: SLF001
        parts = [slot / n for _, n in ordered(p.name for p in slot.glob("messages*.jsonl"))]
        text = b"".join(p.read_bytes() for p in parts).decode()
        want = json.loads((FIXTURES / f"{name}.json").read_text())
        assert normalise(text, session.session_id).splitlines() == want["messages_jsonl"]["lines"]
        log = subprocess.run(
            ["git", "-C", str(session._state.path), "log", "--reverse", "--name-only", "--format=@@%s"],  # noqa: SLF001
            check=True, capture_output=True, text=True,
        ).stdout.split("@@")[1:]
        assert len(log) == len(want["git"])
        for got, golden in zip(log, want["git"]):
            _, *files = [x for x in got.splitlines() if x.strip()] or [""]
            assert sorted({re.sub(r"messages\.\d+\.jsonl", KEY, normalise(f, session.session_id)) for f in files}) == golden["files"]
    finally:
        await session.aclose()
        await backend.aclose()
