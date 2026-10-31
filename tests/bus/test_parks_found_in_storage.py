"""``watch_files`` and MCP task parks are found where they live, in session storage, on every scheduler (core review finding 1).

The lookups read ``InMemoryScheduler._sessions``, which production never fills, so on the zero-config single-process install no watcher was ever
started, a file written over REST woke nothing and an MCP task-style tool was never polled: each park ended only at its deadline.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from primer.bus.mcp_tasks import _find_parked_mcp_task_keys
from primer.bus.watch_notify import wake_watch_files_on_write
from primer.bus.watcher import _find_active_watch_parks
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.scheduler.in_memory import InMemoryScheduler
from tests.conftest import _FakeStorageProvider


def _parked(sid: str, key: str, tool: str, meta: dict, *, status: str = "parked", workspace: str = "ws-1") -> WorkspaceSession:
    now = datetime.now(UTC)
    row = WorkspaceSession(
        id=sid, workspace_id=workspace, binding=AgentSessionBinding(kind="agent", agent_id="ag-x"), status=SessionStatus.RUNNING, created_at=now,
    )
    row.parked_status = status
    row.parked_event_key = key
    row.parked_until = now + timedelta(seconds=600)
    row.parked_at = now
    row.parked_state = {
        "schema_version": 1, "tool_call_id": "tc1",
        "yielded": {"tool_name": tool, "event_key": key, "timeout": 600.0, "resume_metadata": {"workspace_id": workspace, **meta}},
    }
    return row


async def _world():
    sp = _FakeStorageProvider()
    sched = InMemoryScheduler(storage_provider=sp)
    await sched.initialize()
    store = sp.get_storage(WorkspaceSession)
    await store.create(_parked("s-watch", "watch:s-watch:tc1", "watch_files", {"paths": ["a.txt"], "batch_window_ms": 30}))
    await store.create(_parked("s-mcp", "mcp_task:s-mcp:tc1", "mcp_task", {"toolset_id": "ts", "task_id": "t1"}))
    await store.create(_parked("s-other", "evwait:s-other:tc1", "wait_for_event", {}))
    await store.create(_parked("s-done", "watch:s-done:tc1", "watch_files", {"paths": ["b.txt"]}, status="resumable"))
    return sp, sched


async def test_the_in_memory_scheduler_finds_watch_and_mcp_parks_in_storage():
    _, sched = await _world()
    watch = await _find_active_watch_parks(sched)
    mcp = await _find_parked_mcp_task_keys(sched)
    assert [(p["event_key"], p["workspace_id"], p["paths"]) for p in watch] == [("watch:s-watch:tc1", "ws-1", ["a.txt"])]
    assert [(p["event_key"], p["toolset_id"], p["task_id"]) for p in mcp] == [("mcp_task:s-mcp:tc1", "ts", "t1")]


async def test_a_file_write_wakes_the_parked_watch_on_the_in_memory_scheduler():
    _, sched = await _world()

    class _Bus:
        published: list = []

        async def publish(self, key, payload=None):
            self.published.append((key, payload))

    bus = _Bus()
    bus.published = []
    woken = await wake_watch_files_on_write(workspace_id="ws-1", path="a.txt", scheduler=sched, event_bus=bus)
    assert woken == 1 and [k for k, _ in bus.published] == ["watch:s-watch:tc1"]
    assert await wake_watch_files_on_write(workspace_id="ws-1", path="zzz.txt", scheduler=sched, event_bus=bus) == 0
    assert await wake_watch_files_on_write(workspace_id="ws-2", path="a.txt", scheduler=sched, event_bus=bus) == 0


async def test_more_than_200_parks_are_all_found():
    sp = _FakeStorageProvider()
    sched = InMemoryScheduler(storage_provider=sp)
    await sched.initialize()
    store = sp.get_storage(WorkspaceSession)
    for i in range(230):
        await store.create(_parked(f"s{i:03d}", f"watch:s{i:03d}:tc1", "watch_files", {"paths": ["a.txt"]}))
    assert len(await _find_active_watch_parks(sched)) == 230


async def test_a_scheduler_without_storage_finds_nothing_and_does_not_raise():
    sched = InMemoryScheduler()
    await sched.initialize()
    assert await _find_active_watch_parks(sched) == [] and await _find_parked_mcp_task_keys(sched) == []
