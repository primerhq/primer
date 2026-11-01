"""Runtime streams: exec output, watches, lifecycle subscribers and archive (Part 3 Task 121: GI-7, GI-9, GI-12, GI-13).

The stream helpers are driven directly (``run_exec``, ``start_watch``, ``EventBroadcaster``), and the platform client
against the in-process server where the wire matters.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Any

from primer_runtime import exec as exec_mod
from primer_runtime.events_sub import EXEC_KINDS, EventBroadcaster
from primer_runtime.locks import WorkspaceLockTable
from primer_runtime.watch import WatchRegistry, start_watch


async def _exec_events(root: Path, script: str, *, pause_after_first: float = 0.0) -> list[Any]:
    agen = exec_mod.run_exec(1, {"cmd": ["/bin/sh", "-c", script], "access": "read"}, str(root), WorkspaceLockTable())
    events = [await agen.__anext__()]
    await asyncio.sleep(pause_after_first)  # a slow websocket consumer
    events += [event async for event in agen]
    return events


def _stream(events: list[Any], name: str) -> bytes:
    return b"".join(base64.b64decode(e.data["data_b64"]) for e in events if e.event == name)


async def _until(frames: list[dict], pred, within: float = 10.0) -> dict:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        for frame in frames:
            if pred(frame):
                return frame
        await asyncio.sleep(0.02)
    raise AssertionError(f"no matching frame in {frames}")


def _watch(root: Path, frames: list[dict], req_id: int = 1, paths: list[str] | None = None) -> WatchRegistry:
    registry = WatchRegistry()

    async def send(frame: str) -> None:
        frames.append(json.loads(frame))

    start_watch(req_id, {"paths": paths or ["."]}, str(root), send, registry)
    return registry


async def _change_seen(root: Path, frames: list[dict], target: Path) -> dict:
    """Write *target* until its change is reported: the first write may land before the watch is armed."""
    for i in range(50):
        target.write_text(f"v{i}")
        try:
            return await _until(frames, lambda f: f.get("event") == "change" and f["data"]["path"] == str(target), 0.2)
        except AssertionError:
            continue
    raise AssertionError(f"{target} was never reported: {frames}")


# ---------------------------------------------------------------------------
# Characterization: pinned on the unchanged code, unchanged after the fix
# ---------------------------------------------------------------------------


async def test_exec_output_bytes_and_order(tmp_path: Path) -> None:
    script = "seq 1 300000; printf err-1 >&2; printf tail; exit 3"
    expected = "".join(f"{i}\n" for i in range(1, 300001)).encode() + b"tail"
    for pause in (0.0, 1.0):
        events = await _exec_events(tmp_path, script, pause_after_first=pause)
        assert [e.event for e in events[:-1]] == [e.event for e in events if e.event in ("stdout", "stderr")]
        assert events[-1].event == "exit" and events[-1].data == {"code": 3}
        assert _stream(events, "stdout") == expected
        assert _stream(events, "stderr") == b"err-1"
        assert all(e.req_id == 1 for e in events)


async def test_watch_reports_changes_inside_the_root(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    (root / "sub").mkdir(parents=True)
    frames: list[dict] = []
    registry = _watch(root, frames)
    try:
        await _until(frames, lambda f: f.get("event") == "watch_open")
        target = root / "sub" / "a.txt"
        change = await _change_seen(root, frames, target)
        assert change["req_id"] == 1
        assert change["data"]["size"] == target.stat().st_size
        assert change["data"]["mtime"] == target.stat().st_mtime
        assert frames[0] == {"req_id": 1, "event": "watch_open", "data": None, "__type__": "evt"}

        target.unlink()
        gone = await _until(frames, lambda f: f.get("event") == "change" and f["data"]["path"] == str(target)
                            and f["data"]["size"] is None)
        assert gone["data"]["mtime"] is None
    finally:
        registry.cancel_all()
    closed = await _until(frames, lambda f: f.get("event") == "watch_closed")
    assert frames[-1] == closed == {"req_id": 1, "event": "watch_closed", "data": None, "__type__": "evt"}


async def test_lifecycle_events_reach_every_healthy_subscriber() -> None:
    broadcaster = EventBroadcaster()
    got: dict[int, list[dict]] = {7: [], 8: []}

    def sender(req_id: int):
        async def send(frame: str) -> None:
            got[req_id].append(json.loads(frame))
        return send

    for req_id in got:
        broadcaster.subscribe(req_id=req_id, kinds=EXEC_KINDS, send=sender(req_id))
    broadcaster.subscribe(req_id=9, kinds=frozenset({"exec_exited"}), send=sender(7))  # a kind filter

    await broadcaster.broadcast("exec_started", {"cmd": ["true"], "workdir": None})
    await broadcaster.broadcast("exec_exited", {"cmd": ["true"], "exit_code": 0, "duration_ms": 5})
    for _ in range(20):
        await asyncio.sleep(0)

    started = {"kind": "exec_started", "cmd": ["true"], "workdir": None}
    exited = {"kind": "exec_exited", "cmd": ["true"], "exit_code": 0, "duration_ms": 5}
    assert [(f["req_id"], f["event"], f["data"]) for f in got[8]] == [(8, "ws_event", started), (8, "ws_event", exited)]
    assert [(f["req_id"], f["data"]) for f in got[7]] == [(7, started), (7, exited), (9, exited)]


async def test_a_subscriber_whose_send_fails_is_dropped() -> None:
    broadcaster = EventBroadcaster()
    calls: list[str] = []

    async def gone(frame: str) -> None:
        calls.append(frame)
        raise ConnectionResetError("gone")

    broadcaster.subscribe(req_id=1, kinds=EXEC_KINDS, send=gone)
    await broadcaster.broadcast("exec_started", {"cmd": ["a"]})
    for _ in range(20):
        await asyncio.sleep(0)
    await broadcaster.broadcast("exec_exited", {"cmd": ["a"]})
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(calls) == 1

