"""WorkspaceEventBridge: opt-in scan, kind filtering, emission, GC."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

import pytest_asyncio

from primer.events.workspace_bridge import WorkspaceEventBridge
from primer.model.provider import SqliteConfig
from primer.model.workspace import (
    Workspace,
    WorkspaceEventsConfig,
    WorkspaceRuntimeMeta,
)
from primer.storage.sqlite import SqliteStorageProvider
from pydantic import SecretStr

# asyncio_mode = "auto" in pyproject.toml: async tests need no marker.


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "t.sqlite")))
    await provider.initialize()
    await provider.get_event_store().ensure_schema()
    try:
        yield provider
    finally:
        await provider.aclose()


def _workspace(ws_id: str, events: WorkspaceEventsConfig | None) -> Workspace:
    return Workspace(
        id=ws_id, description="bridge probe",
        template_id="tpl-1", provider_id="p-1",
        created_at=datetime.now(timezone.utc),
        runtime_meta=WorkspaceRuntimeMeta(
            url="ws://127.0.0.1:1/", token=SecretStr("t"),
        ),
        events=events,
    )


class _ScriptedResolver:
    """Feeds a fixed list of items once, then blocks (a live stream)."""

    def __init__(self, items: list[dict]) -> None:
        self._items = items
        self.resolved: list[str] = []

    async def __call__(self, workspace_id: str, config):
        self.resolved.append(workspace_id)
        items = list(self._items)

        async def _stream():
            for item in items:
                yield item
            await asyncio.Event().wait()  # stay open like a real stream

        return _stream()


async def _drain_until(sp, count: int, *, timeout: float = 5.0) -> list:
    store = sp.get_event_store()
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        events = await store.read_after(0, event_type_prefix="workspace.")
        if len(events) >= count:
            return events
        await asyncio.sleep(0.05)
    raise AssertionError(f"never saw {count} workspace events")


async def test_opted_in_workspace_streams_onto_the_log(sp):
    await sp.get_storage(Workspace).create(_workspace(
        "ws-on", WorkspaceEventsConfig(),
    ))
    await sp.get_storage(Workspace).create(_workspace("ws-off", None))

    resolver = _ScriptedResolver([
        {"kind": "file_changed", "path": "src/x.py", "mtime": 1.0,
         "size": 10},
        {"kind": "exec_started", "cmd": ["make", "build"],
         "workdir": None},
        {"kind": "exec_exited", "cmd": ["make", "build"],
         "exit_code": 0, "duration_ms": 12},
        {"kind": "not_a_kind", "x": 1},
    ])
    bridge = WorkspaceEventBridge(
        storage_provider=sp, stream_resolver=resolver,
        scan_interval_seconds=0.05,
    )
    bridge.start()
    try:
        events = await _drain_until(sp, 3)
        assert [e.event_type for e in events] == [
            "workspace.file_changed",
            "workspace.exec_started",
            "workspace.exec_exited",
        ]
        assert all(e.workspace_id == "ws-on" for e in events)
        assert events[0].payload["path"] == "src/x.py"
        assert events[2].payload["exit_code"] == 0
        # The opted-out workspace never resolved a stream.
        assert set(resolver.resolved) == {"ws-on"}
    finally:
        await bridge.stop()


async def test_config_kinds_filter_applies(sp):
    await sp.get_storage(Workspace).create(_workspace(
        "ws-files", WorkspaceEventsConfig(kinds=["file_changed"]),
    ))
    resolver = _ScriptedResolver([
        {"kind": "exec_started", "cmd": ["x"]},
        {"kind": "file_changed", "path": "a"},
    ])
    bridge = WorkspaceEventBridge(
        storage_provider=sp, stream_resolver=resolver,
        scan_interval_seconds=0.05,
    )
    bridge.start()
    try:
        events = await _drain_until(sp, 1)
        assert [e.event_type for e in events] == ["workspace.file_changed"]
    finally:
        await bridge.stop()


async def test_opt_out_stops_the_stream(sp):
    workspaces = sp.get_storage(Workspace)
    await workspaces.create(_workspace("ws-flip", WorkspaceEventsConfig()))
    resolver = _ScriptedResolver([{"kind": "file_changed", "path": "a"}])
    bridge = WorkspaceEventBridge(
        storage_provider=sp, stream_resolver=resolver,
        scan_interval_seconds=0.05,
    )
    bridge.start()
    try:
        await _drain_until(sp, 1)
        row = await workspaces.get("ws-flip")
        await workspaces.update(row.model_copy(update={"events": None}))
        deadline = asyncio.get_running_loop().time() + 3.0
        while asyncio.get_running_loop().time() < deadline:
            if not bridge._streams:
                break
            await asyncio.sleep(0.05)
        assert not bridge._streams
    finally:
        await bridge.stop()
