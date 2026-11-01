"""Runtime streams: exec output, watches, lifecycle subscribers and archive (Part 3 Task 121: GI-7, GI-9, GI-12, GI-13).

The stream helpers are driven directly (``run_exec``, ``start_watch``, ``EventBroadcaster``), and the platform client
against the in-process server where the wire matters.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from primer_runtime import exec as exec_mod
from primer_runtime.events_sub import EXEC_KINDS, EventBroadcaster
from primer_runtime.locks import WorkspaceLockTable
from primer_runtime.watch import WatchRegistry, start_watch
from tests.runtime.test_runtime_freezes import _client, runtime  # noqa: F401 - the in-process server fixture


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



# ---------------------------------------------------------------------------
# GI-7: exec output pushes back on the command instead of piling up
# ---------------------------------------------------------------------------


async def test_a_slow_consumer_holds_at_most_256_exec_events(tmp_path: Path, monkeypatch) -> None:
    peak = {"n": 0}

    class RecordingQueue(asyncio.Queue):
        def put_nowait(self, item):
            super().put_nowait(item)
            peak["n"] = max(peak["n"], self.qsize())

    monkeypatch.setattr(exec_mod.asyncio, "Queue", RecordingQueue)
    events = await _exec_events(tmp_path, "head -c 20000000 /dev/zero", pause_after_first=2.0)
    assert _stream(events, "stdout") == bytes(20000000)
    assert events[-1].data == {"code": 0}
    assert peak["n"] <= 256


# ---------------------------------------------------------------------------
# GI-9: a watch is built off the loop and stays inside the workspace
# ---------------------------------------------------------------------------


def _spy_watcher_builds(monkeypatch, build) -> None:
    import watchfiles.main

    monkeypatch.setattr(watchfiles.main, "RustNotify", build)  # where awatch looks it up


async def test_a_change_behind_a_link_that_leaves_the_root_is_not_reported(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    (outside / "d").mkdir(parents=True)
    root = tmp_path / "ws"
    root.mkdir()
    os.symlink(outside, root / "escape")
    frames: list[dict] = []
    registry = _watch(root, frames)
    try:
        await _until(frames, lambda f: f.get("event") == "watch_open")
        (outside / "d" / "secret.txt").write_text("x")  # changed outside the workspace, seen through the link
        await _change_seen(root, frames, root / "inside.txt")
        (outside / "d" / "secret.txt").write_text("y")
        await _change_seen(root, frames, root / "inside.txt")
    finally:
        registry.cancel_all()
    await _until(frames, lambda f: f.get("event") == "watch_closed")
    assert [f["data"]["path"] for f in frames if f.get("event") == "change" and "secret" in f["data"]["path"]] == []


async def test_the_recursive_watcher_is_built_off_the_loop(tmp_path: Path, monkeypatch) -> None:
    from watchfiles._rust_notify import RustNotify

    threads: list[threading.Thread] = []

    def build(*args):
        threads.append(threading.current_thread())
        return RustNotify(*args)

    _spy_watcher_builds(monkeypatch, build)
    frames: list[dict] = []
    registry = _watch(tmp_path, frames)
    try:
        await _until(frames, lambda f: f.get("event") == "watch_open")
        await _change_seen(tmp_path, frames, tmp_path / "a.txt")
    finally:
        registry.cancel_all()
    await _until(frames, lambda f: f.get("event") == "watch_closed")
    assert len(threads) == 1
    assert threads[0] is not threading.current_thread()  # this test runs on the loop's thread


async def test_a_watch_whose_setup_fails_sends_watch_error(tmp_path: Path, monkeypatch) -> None:
    def build(*args):
        raise OSError(28, "OS file watch limit reached")

    _spy_watcher_builds(monkeypatch, build)
    frames: list[dict] = []
    registry = _watch(tmp_path, frames, req_id=4)
    try:
        await _until(frames, lambda f: f.get("event") == "watch_closed")
    finally:
        registry.cancel_all()
    assert [f["event"] for f in frames] == ["watch_error", "watch_closed"]
    assert frames[0]["req_id"] == 4
    assert frames[0]["data"]["code"] == "EINTERNAL"
    assert "OS file watch limit reached" in frames[0]["data"]["message"]


# ---------------------------------------------------------------------------
# GI-12: one stalled lifecycle subscriber holds up nobody
# ---------------------------------------------------------------------------


async def test_a_stalled_subscriber_does_not_hold_up_the_broadcast_or_the_others() -> None:
    broadcaster = EventBroadcaster()
    never = asyncio.Event()
    got: list[dict] = []

    async def stalled(frame: str) -> None:
        await never.wait()  # a subscriber whose socket is back-pressured

    async def healthy(frame: str) -> None:
        got.append(json.loads(frame))

    broadcaster.subscribe(req_id=1, kinds=EXEC_KINDS, send=stalled)
    broadcaster.subscribe(req_id=2, kinds=EXEC_KINDS, send=healthy)
    task = asyncio.ensure_future(broadcaster.broadcast("exec_started", {"cmd": ["true"]}))
    for _ in range(5):
        await asyncio.sleep(0)
    assert task.done()
    assert [f["data"]["kind"] for f in got] == ["exec_started"]


async def test_an_exec_runs_while_a_subscriber_is_stalled(tmp_path: Path) -> None:
    broadcaster = EventBroadcaster()
    never = asyncio.Event()

    async def stalled(frame: str) -> None:
        await never.wait()

    broadcaster.subscribe(req_id=1, kinds=EXEC_KINDS, send=stalled)
    frames: list[dict] = []

    async def send(frame: str) -> None:
        frames.append(json.loads(frame))

    task = exec_mod.start_exec(
        5, {"cmd": ["/bin/sh", "-c", "echo ran"], "access": "read"}, str(tmp_path), WorkspaceLockTable(), send,
        exec_mod.ExecRegistry(), broadcaster=broadcaster,
    )
    try:
        await asyncio.wait_for(task, timeout=10)
    finally:
        task.cancel()
    assert frames[-1]["event"] == "exit" and frames[-1]["data"] == {"code": 0}


async def test_a_subscriber_that_falls_behind_loses_events_counted_and_logged(caplog) -> None:
    broadcaster = EventBroadcaster()
    never = asyncio.Event()

    async def stalled(frame: str) -> None:
        await never.wait()

    broadcaster.subscribe(req_id=3, kinds=EXEC_KINDS, send=stalled)
    with caplog.at_level("WARNING", logger="primer_runtime.events_sub"):
        for i in range(300):
            await asyncio.wait_for(broadcaster.broadcast("exec_exited", {"cmd": [str(i)]}), 5)
            await asyncio.sleep(0)
    dropped = [r for r in caplog.records if "dropped" in r.getMessage()]
    assert dropped, caplog.text
    # one frame is in the stalled send, 256 wait in its queue, the rest are dropped
    assert dropped[-1].getMessage().endswith("(43 dropped so far)")


# ---------------------------------------------------------------------------
# GI-13: the client's archive() raises on the runtime's refusal
# ---------------------------------------------------------------------------


async def test_archive_raises_on_an_error_frame(runtime) -> None:  # noqa: F811
    from primer.workspace.runtime.runtime_client import RuntimeError as RtError

    _connect, _root, url = runtime
    client = await _client(url)
    try:
        async with asyncio.timeout(10):
            try:
                async for _chunk in client.archive(["."]):
                    pass
            except RtError as exc:
                assert exc.code == "EUNSUPPORTED"
                assert "archive" in str(exc)
            else:
                raise AssertionError("archive() ended without the runtime's error")
    finally:
        await client.aclose()
