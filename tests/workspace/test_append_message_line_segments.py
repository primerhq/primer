"""Event rows follow the ACTIVE segment of a session log and never rotate it (seglog Task 10; design 4.2 and 4.4).

``append_message_line`` is the event-row writer (a steer, the turn's records). It lists the slot and appends to the newest segment at that moment, under the session's
messages lock and, locally, the per-session ``flock``; rotation belongs to ``LocalStateRepo.commit`` alone, which takes the same ``flock`` around resolve, rotate and append.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from primer.workspace.local import state as state_mod
from primer.workspace.segments import ordered, set_segment_bytes_default
from tests._support.off_golden import open_session

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git CLI not available on PATH")

KEY = "messages.jsonl"


@pytest.fixture(autouse=True)
def _reset_threshold():
    yield
    set_segment_bytes_default(None)


@pytest.fixture
async def world(tmp_path):
    backend, workspace, session = await open_session(tmp_path)
    try:
        yield World(workspace, session)
    finally:
        await session.aclose()
        await backend.aclose()


class World:
    def __init__(self, workspace, session) -> None:
        self.workspace, self.session = workspace, session
        self.state = session._state  # noqa: SLF001
        self.sid = session.session_id
        self.slot = self.state.path / "sessions" / self.sid

    async def commit(self, data: bytes, summary: str = "turn") -> str:
        return await self.state.commit(self.sid, summary=summary, op="message", appends={KEY: data})

    async def row(self, data: bytes) -> None:
        await self.workspace.append_message_line(self.sid, data)

    def names(self) -> list[str]:
        return [name for _, name in ordered(p.name for p in self.slot.glob("messages*.jsonl"))]

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.state.path), *args], check=True, capture_output=True, text=True).stdout


def line(tag: str, size: int = 40) -> bytes:
    return (json.dumps({"role": "user", "parts": [{"type": "text", "text": f"{tag}:" + "x" * size}]}) + "\n").encode()


async def test_event_rows_go_to_the_active_segment_after_a_rotation(world):
    set_segment_bytes_default(300)
    await world.commit(line("a", 400))
    await world.commit(line("b"), summary="rotating")
    assert world.names() == [KEY, "messages.0001.jsonl"]
    before = (world.slot / KEY).read_bytes()
    await world.row(line("event"))
    assert (world.slot / KEY).read_bytes() == before, "the closed segment is left alone"
    assert (world.slot / "messages.0001.jsonl").read_bytes() == line("b") + line("event")


async def test_a_session_with_no_log_yet_starts_segment_zero(world):
    (world.slot / KEY).unlink()
    await world.row(line("first"))
    assert world.names() == [KEY] and (world.slot / KEY).read_bytes() == line("first")


async def test_event_rows_never_rotate_even_far_past_the_threshold(world):
    set_segment_bytes_default(100)
    for n in range(20):
        await world.row(line(f"r{n}", 500))
    assert world.names() == [KEY], "only a commit rotates"
    assert (world.slot / KEY).read_bytes().endswith(line("r19", 500))


async def test_a_commit_stages_event_rows_appended_before_the_rotation(world):
    set_segment_bytes_default(300)
    await world.commit(line("a", 400))
    await world.row(line("event1"))
    await world.row(line("event2"))
    await world.commit(line("b"), summary="rotating")
    assert world.names() == [KEY, "messages.0001.jsonl"]
    in_git = world.git("show", f"HEAD:sessions/{world.sid}/{KEY}").encode()
    assert in_git.endswith(line("event1") + line("event2")), "the closed segment in git holds the rows appended before the rotation"
    assert world.git("status", "--porcelain").strip() == ""


async def test_the_lock_file_is_git_ignored(world):
    await world.row(line("event"))
    await world.commit(line("b"))
    assert (world.slot / state_mod.LOCK_NAME).exists()
    assert world.git("status", "--porcelain").strip() == ""
    assert state_mod.LOCK_NAME not in world.git("ls-files", "--", f"sessions/{world.sid}")


async def test_flock_is_taken_by_both_paths(world, monkeypatch):
    taken: list[tuple[str, Path]] = []
    real = state_mod.log_slot_lock

    def spy(slot):
        taken.append((spy.who, Path(slot)))
        return real(slot)

    monkeypatch.setattr(state_mod, "log_slot_lock", spy)
    spy.who = "append"
    await world.row(line("event"))
    spy.who = "commit"
    await world.commit(line("b"))
    assert taken == [("append", world.slot), ("commit", world.slot)]


async def test_a_commit_that_does_not_touch_the_log_takes_no_lock(world, monkeypatch):
    monkeypatch.setattr(state_mod, "log_slot_lock", lambda slot: pytest.fail("a commit of another file needs no log lock"))
    await world.state.commit(world.sid, summary="notes", op="message", files={"notes.md": "hi"})


class _StubSandbox:
    """The two sandbox ops an event row needs: ``list_dir`` (absolute paths, as the runtime answers) and ``append_line``."""

    def __init__(self, files: dict[str, bytes], *, list_error: Exception | None = None) -> None:
        self.files = files
        self.list_error = list_error

    async def stat(self, path: str):
        from types import SimpleNamespace

        return SimpleNamespace(kind="dir") if any(p.startswith(path + "/") for p in self.files) else None

    async def list_dir(self, path: str):
        from types import SimpleNamespace

        if self.list_error is not None:
            raise self.list_error
        return [SimpleNamespace(path=p, kind="file") for p in self.files if p.rsplit("/", 1)[0] == path]

    async def append_line(self, path: str, line: bytes) -> None:
        self.files[path] = self.files.get(path, b"") + line + b"\n"


def _sandbox_workspace(sandbox: _StubSandbox):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from primer.workspace.sandbox.workspace import SandboxWorkspace

    @asynccontextmanager
    async def _lock():
        yield

    ws = object.__new__(SandboxWorkspace)
    ws._sandbox, ws._workspace_root = sandbox, "/workspace"  # noqa: SLF001
    ws._template = SimpleNamespace(state_path=".state")  # noqa: SLF001
    ws._state_repo = SimpleNamespace(messages_lock=lambda sid: _lock())  # noqa: SLF001
    return ws


_SLOT = "/workspace/.state/sessions/s1"


def _rotated() -> dict[str, bytes]:
    return {f"{_SLOT}/{KEY}": b"a\n", f"{_SLOT}/messages.0001.jsonl": b"b\n", f"{_SLOT}/messages.0002.jsonl": b"c\n", f"{_SLOT}/agent.json": b"{}"}


async def test_a_sandbox_event_row_goes_to_the_listed_active_segment():
    sandbox = _StubSandbox(_rotated())
    ws = _sandbox_workspace(sandbox)
    await ws.append_message_line("s1", b"row\n")
    assert sandbox.files[f"{_SLOT}/messages.0002.jsonl"] == b"c\nrow\n" and sandbox.files[f"{_SLOT}/{KEY}"] == b"a\n"
    await ws.append_message_line("fresh", b"first\n")   # no directory yet: segment 0
    assert sandbox.files["/workspace/.state/sessions/fresh/messages.jsonl"] == b"first\n"


async def test_a_failing_listing_fails_the_append_instead_of_landing_in_a_closed_segment():
    """Only an ABSENT slot means segment 0: a runtime hiccup on a rotated log must not put the row in the closed ``messages.jsonl``."""
    files = _rotated()
    sandbox = _StubSandbox(files, list_error=OSError("runtime timed out"))
    ws = _sandbox_workspace(sandbox)
    with pytest.raises(OSError):
        await ws.append_message_line("s1", b"row\n")
    assert files == _rotated(), "nothing was appended anywhere"
