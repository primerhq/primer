"""An event-row writer in ANOTHER process against rotating commits (seglog Task 10; design 4.4, Review Focus 5).

The API process writes a steer's record while a worker commits a turn. Both resolve the active segment under the session's ``flock``, so a row can never land in a
segment after the commit that closed it staged it, and none is lost or reordered.
"""

from __future__ import annotations

import json
import multiprocessing
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from primer.workspace.segments import ordered, set_segment_bytes_default
from tests._support.off_golden import open_session

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git CLI not available on PATH")

KEY = "messages.jsonl"
ITERATIONS = 200
THRESHOLD = 1024


def _row(who: str, i: int) -> bytes:
    return (json.dumps({"role": "user", "parts": [{"type": "text", "text": f"{who}-{i:04d}-" + "y" * 90}]}) + "\n").encode()


def _writer_main(state_dir: str, workspace_id: str, session_id: str, count: int, ready, first_done, go) -> None:
    """The other process: its own repo object over the same directory, writing event rows through the real ``append_message_line``.

    The rows go out in ``len(go) + 1`` phases: the first at once (the log has not rotated yet), each later one only after the committing process has
    rotated the log one more time and said so (``go[k]``); a phase then runs WHILE the parent keeps committing, which is the race. Not timing: under CPU
    contention the interleaving is the same.
    """
    import asyncio

    from primer.workspace.local.state import LocalStateRepo
    from primer.workspace.local.workspace import LocalWorkspace

    async def main() -> None:
        repo = LocalStateRepo(Path(state_dir), workspace_id=workspace_id)
        await repo.initialize()
        workspace = object.__new__(LocalWorkspace)
        workspace._root = Path(state_dir).parent  # noqa: SLF001
        workspace._template = SimpleNamespace(state_path=Path(state_dir).name)  # noqa: SLF001
        workspace._state = repo  # noqa: SLF001
        ready.set()
        per_phase = count // (len(go) + 1)
        for i in range(count):
            if i == per_phase:
                first_done.set()
            if i >= per_phase and (i - per_phase) % per_phase == 0:
                assert go[(i - per_phase) // per_phase].wait(60), "the committing process never rotated the log again"
            await workspace.append_message_line(session_id, _row("event", i))
        first_done.set()

    asyncio.run(main())


async def test_race_append_vs_rotating_commit_across_two_processes(tmp_path):
    backend, workspace, session = await open_session(tmp_path)
    try:
        state = session._state  # noqa: SLF001
        sid = session.session_id
        slot = state.path / "sessions" / sid
        set_segment_bytes_default(THRESHOLD)
        ctx = multiprocessing.get_context("spawn")
        ready, first_done = ctx.Event(), ctx.Event()
        go = [ctx.Event() for _ in range(3)]
        child = ctx.Process(target=_writer_main, args=(str(state.path), "ws-race", sid, ITERATIONS, ready, first_done, go))
        child.start()
        try:
            assert ready.wait(60), "the writer process did not start"
            assert first_done.wait(60), "the writer never finished its first phase"   # those rows are in segment 0: no commit has rotated the log yet
            for i in range(ITERATIONS):
                await state.commit(sid, summary=f"turn {i}", op="message", appends={KEY: _row("turn", i)})
                rotations = len(list(slot.glob("messages*.jsonl"))) - 1
                for k, event in enumerate(go):
                    if rotations > k:
                        event.set()               # the next phase of event rows starts now, while this loop keeps committing
        finally:
            for event in go:
                event.set()
            child.join(timeout=90)
            if child.is_alive():
                child.kill()
                child.join()
        assert child.exitcode == 0
        await state.commit(sid, summary="final", op="message", appends={KEY: _row("turn", ITERATIONS)})

        names = [n for _, n in ordered(p.name for p in slot.glob("messages*.jsonl"))]
        assert len(names) >= 3, "the 1 KB threshold rotated the log several times"
        texts = {n: (slot / n).read_text() for n in names}
        every = re.findall(r"(turn|event)-(\d{4})-", "".join(texts[n] for n in names))
        assert sorted(f"{w}-{i}" for w, i in every) == sorted([f"turn-{i:04d}" for i in range(ITERATIONS + 1)] + [f"event-{i:04d}" for i in range(ITERATIONS)]), "no row lost or doubled"
        spread = sum(1 for n in names if "event-" in texts[n])
        assert spread >= 2, "the writer ran across at least one rotation, or this test proves nothing"
        for who in ("turn", "event"):
            seen = [int(i) for w, i in every if w == who]
            assert seen == sorted(seen), f"{who} rows keep their order across the segments"
        # a closed segment is complete in the commit that closed it: what git held when the NEXT segment appeared is what is on disk now
        for closed, nxt in zip(names, names[1:]):
            first = subprocess.run(
                ["git", "-C", str(state.path), "log", "--diff-filter=A", "--format=%H", "--", f"sessions/{sid}/{nxt}"],
                check=True, capture_output=True, text=True,
            ).stdout.split()[-1]
            staged = subprocess.run(
                ["git", "-C", str(state.path), "show", f"{first}:sessions/{sid}/{closed}"], check=True, capture_output=True, text=True,
            ).stdout
            assert staged == texts[closed], f"{closed} changed after the commit that closed it"
        status = subprocess.run(["git", "-C", str(state.path), "status", "--porcelain"], check=True, capture_output=True, text=True).stdout
        assert status.strip() == ""
    finally:
        await session.aclose()
        await backend.aclose()
