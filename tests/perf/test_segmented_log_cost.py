"""Seglog Task 13: the cost of a session's log stays flat as the log grows (segmented session log, design D3, section 6).

A real local state repo (git) grows a log to 20 MiB in 200 turns of ~100 KB each, with append-only writes and a 1 MiB segment threshold. What is asserted is the point of
segmentation, with thresholds generous for CI: what git STORES per commit is bounded by the threshold, not by the size of the log (a single file stores the whole log again
for every commit); a commit does not take longer at turn 200 than at turn 20; and the cold read of a compacted log reads only the segments from the compaction marker on.
The numbers are printed (``-s``) for the report. A second test prints what the segment-aware code asks of the disk per turn with rotation OFF (what PR A adds to every session).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

import primer.workspace.local.state as state_mod
from primer.model.workspace_session import SessionMessageKind, SessionMessageRecord
from primer.workspace.segments import SessionLog, WorkspaceSource, ordered, set_segment_bytes_default
from tests._support.off_golden import open_session

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git CLI not available on PATH")

KEY = "messages.jsonl"
MIB = 1024 * 1024
TURNS = 200
TURN_BYTES = 100 * 1024          # 200 turns of ~100 KiB: a 20 MiB log
THRESHOLD = MIB


@pytest.fixture(autouse=True)
def _reset_threshold():
    yield
    set_segment_bytes_default(None)


def _turn(n: int) -> bytes:
    """One turn's batch of lines: ~TURN_BYTES, incompressible (git compresses what it stores), several records."""
    per = TURN_BYTES // 4
    return b"".join(
        (f'{{"role":"user","parts":[{{"type":"text","text":"t{n}-{k}-{os.urandom(per // 2).hex()}"}}]}}\n').encode() for k in range(4)
    )


def _objects_bytes(repo: Path) -> int:
    total = 0
    for f in (repo / ".git" / "objects").rglob("*"):
        if not f.is_file():
            continue
        try:
            total += f.stat().st_size
        except FileNotFoundError:
            # git's background maintenance removed it between the is_file and the stat (a loose object being packed, its per-run maintenance.lock); its bytes are in the pack now or were never log bytes
            pass
    return total


def _marker() -> bytes:
    rec = SessionMessageRecord(seq=1, kind=SessionMessageKind.COMPACTION_MARKER, payload={"summary": "s"}, created_at=datetime(2026, 11, 2, tzinfo=timezone.utc))
    return (rec.model_dump_json() + "\n").encode()


class _CountingSource(WorkspaceSource):
    def __init__(self, workspace) -> None:
        super().__init__(workspace)
        self.bytes_read = 0

    async def read(self, rel: str):
        data = await super().read(rel)
        self.bytes_read += len(data or b"")
        return data


def test_objects_bytes_tolerates_a_file_removed_between_the_is_file_and_the_stat(tmp_path, monkeypatch):
    """The CI race (PR #1020, job 128860946502) in its deterministic form: git's background maintenance holds .git/objects/maintenance.lock during each auto-maintenance run and removes it on exit. The victim below survives the walker's is_file probe and is removed on the walker's own stat - exactly the interleaving that crashed the pre-fix walker with a FileNotFoundError."""
    objects = tmp_path / ".git" / "objects"
    (objects / "ab").mkdir(parents=True)
    keep = objects / "ab" / "cd01"
    keep.write_bytes(b"x" * 4096)
    victim = objects / "maintenance.lock"
    victim.write_bytes(b"held")
    real_stat = os.stat
    counts = {"victim": 0}

    def racing_stat(path, *a, **kw):
        if Path(path) == victim:
            counts["victim"] += 1
            if counts["victim"] == 2:      # 1 = the is_file probe (wins the race), 2 = the walker's stat (loses it)
                victim.unlink()
        return real_stat(path, *a, **kw)

    monkeypatch.setattr(os, "stat", racing_stat)
    assert _objects_bytes(tmp_path) == keep.stat().st_size


async def test_cost_per_commit_stays_flat_as_the_log_grows(tmp_path):
    set_segment_bytes_default(THRESHOLD)
    backend, workspace, session = await open_session(tmp_path)
    try:
        state = session._state  # noqa: SLF001
        sid = session.session_id
        slot = state.path / "sessions" / sid
        stored: list[int] = []
        seconds: list[float] = []
        marker_turn = TURNS // 2
        for n in range(TURNS):
            before = _objects_bytes(state.path)
            started = time.perf_counter()
            batch = _turn(n) + (_marker() if n == marker_turn else b"")
            await state.commit(sid, summary=f"turn {n}", op="message", appends={KEY: batch})
            seconds.append(time.perf_counter() - started)
            stored.append(_objects_bytes(state.path) - before)

        names = [name for _, name in ordered(p.name for p in slot.glob("messages*.jsonl"))]
        sizes = {name: (slot / name).stat().st_size for name in names}
        log_size = sum(sizes.values())
        early, late = statistics.median(seconds[15:25]), statistics.median(seconds[TURNS - 10 : TURNS])
        steady = stored[TURNS // 4 :]
        print(
            f"\nSEGLOG PERF log={log_size / MIB:.1f} MiB in {len(names)} segments (largest {max(sizes.values()) / MIB:.2f} MiB); "
            f"git stored per commit: median {statistics.median(steady) / 1024:.0f} KiB, max {max(steady) / 1024:.0f} KiB (threshold {THRESHOLD // 1024} KiB, log {log_size // 1024} KiB); "
            f"commit time: turn 20 {early * 1000:.0f} ms, turn 200 {late * 1000:.0f} ms (x{late / early:.2f})"
        )
        assert log_size >= 18 * MIB, "the log really grew to ~20 MiB"
        assert len(names) >= 15, "it rotated about every 1 MiB"
        assert max(sizes.values()) <= THRESHOLD + 2 * TURN_BYTES, "no segment grows far past the threshold (a batch is never split)"
        # (a) what git stores per commit is bounded by the threshold, not by the log: a single file would store ~the whole log again at the end
        assert max(steady) <= THRESHOLD + 2 * TURN_BYTES, "a commit stored more than about one segment"
        assert max(steady) < log_size / 8
        # (b) the wall time of a commit does not grow with the log
        assert late <= max(3 * early, early + 0.25), f"commit time grew with the log: {early:.3f}s -> {late:.3f}s"

        # (c) the cold read of a compacted log reads only the segments from the one that holds the newest marker on
        marker_segment = next(name for name in names if _marker().strip() in (slot / name).read_bytes())
        from_marker = names[names.index(marker_segment) :]
        source = _CountingSource(workspace)
        offset, data = await SessionLog(source, f"{workspace.template.state_path}/sessions/{sid}").since_last_marker()
        print(
            f"SEGLOG PERF cold read after the compaction (marker in {marker_segment}): read {source.bytes_read / MIB:.1f} MiB of {log_size / MIB:.1f} MiB, "
            f"starting at logical offset {offset}"
        )
        assert source.bytes_read == sum(sizes[name] for name in from_marker) == len(data)
        assert offset == sum(sizes[name] for name in names[: names.index(marker_segment)])
        assert source.bytes_read < log_size * 0.6
    finally:
        await session.aclose()
        await backend.aclose()


async def test_without_rotation_a_commit_stores_the_whole_log_again(tmp_path):
    """The contrast that gives the first test its meaning (smaller: 50 turns, a 5 MiB log): a single file's blob is stored in full by every commit."""
    set_segment_bytes_default(0)
    backend, workspace, session = await open_session(tmp_path)
    try:
        state = session._state  # noqa: SLF001
        stored: list[int] = []
        for n in range(50):
            before = _objects_bytes(state.path)
            await state.commit(session.session_id, summary=f"turn {n}", op="message", appends={KEY: _turn(n)})
            stored.append(_objects_bytes(state.path) - before)
        print(f"\nSEGLOG PERF rotation OFF, 5 MiB log: git stored per commit grows to {stored[-1] / 1024:.0f} KiB (turn 1: {stored[0] / 1024:.0f} KiB)")
        assert stored[-1] > 1.5 * THRESHOLD > 0, "a single file stores about the whole log at every commit: this is what segmentation removes"
    finally:
        await session.aclose()
        await backend.aclose()


async def test_what_the_segment_aware_code_asks_of_the_disk_per_turn_with_rotation_off(tmp_path, monkeypatch):
    """PR A ships with rotation OFF, so every session pays the new bookkeeping without gaining a segment: count and time it. Nothing here is tight."""
    set_segment_bytes_default(0)
    backend, workspace, session = await open_session(tmp_path)
    try:
        state = session._state  # noqa: SLF001
        sid = session.session_id
        slot = state.path / "sessions" / sid
        for n in range(30):                                   # a 3 MiB log, one segment
            await state.commit(sid, summary=f"turn {n}", op="message", appends={KEY: _turn(n)})

        counts: dict[str, int] = {"listdir": 0, "stat": 0, "flock": 0, "open": 0, "git": 0}
        real = {"listdir": os.listdir, "stat": os.stat, "open": os.open, "flock": state_mod.fcntl.flock, "exec": asyncio.create_subprocess_exec}

        def counting(name):
            def wrapper(*a, **kw):
                counts[name] += 1
                return real[name](*a, **kw)
            return wrapper

        async def counting_exec(*a, **kw):
            counts["git"] += 1
            return await real["exec"](*a, **kw)

        def tally(label: str, runs: int) -> str:
            per = {k: v / runs for k, v in counts.items()}
            return f"{label}: " + ", ".join(f"{k} {v:.1f}" for k, v in per.items())

        async def measure(label: str, op, runs: int) -> float:
            for key in counts:
                counts[key] = 0
            monkeypatch.setattr(os, "listdir", counting("listdir"))
            monkeypatch.setattr(os, "stat", counting("stat"))
            monkeypatch.setattr(os, "open", counting("open"))
            monkeypatch.setattr(state_mod.fcntl, "flock", counting("flock"))
            monkeypatch.setattr(asyncio, "create_subprocess_exec", counting_exec)
            started = time.perf_counter()
            for i in range(runs):
                await op(i)
            elapsed = (time.perf_counter() - started) / runs
            monkeypatch.undo()
            print(f"SEGLOG PERF per op, rotation OFF, {tally(label, runs)}, {elapsed * 1e6:.0f} us")
            return elapsed

        row = (b'{"role":"user","parts":[{"type":"text","text":"event"}]}\n')

        async def legacy_row(i: int) -> None:                 # what an event row cost before: one O_APPEND write
            def _w() -> None:
                with (slot / KEY).open("ab") as fh:
                    fh.write(row)
            await asyncio.to_thread(_w)

        async def new_row(i: int) -> None:
            await workspace.append_message_line(sid, row)

        async def commit_append(i: int) -> None:
            await state.commit(sid, summary="turn", op="message", appends={KEY: _turn(100 + i)})

        async def commit_whole(i: int) -> None:                # the whole file rewritten, as with append-only OFF
            current = (slot / KEY).read_bytes()
            await state.commit(sid, summary="turn", op="message", files={KEY: current + _turn(200 + i)})

        legacy = await measure("event row, before (one O_APPEND write)", legacy_row, 300)
        now = await measure("event row, now (list the slot, flock, append)", new_row, 300)
        await measure("commit with an appended batch, now", commit_append, 10)
        await measure("commit with the whole log rewritten, now", commit_whole, 5)
        print(f"SEGLOG PERF an event row costs {max(now - legacy, 0) * 1e6:.0f} us more than before ({legacy * 1e6:.0f} us -> {now * 1e6:.0f} us)")
        assert now < legacy + 0.05, "an event row now costs at most a few extra syscalls (generous bound)"
    finally:
        await session.aclose()
        await backend.aclose()
