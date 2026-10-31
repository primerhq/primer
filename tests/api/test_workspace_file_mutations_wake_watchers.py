"""Every door that changes the workspace's files wakes a ``watch_files`` park (review of workspaces, W55h).

``PUT /files`` called ``wake_watch_files_on_write``; the REST delete, move and mkdir routes and the MCP ``write_workspace_file`` / ``delete_workspace_file``
tools did not, so a session parked on a watched path stayed asleep until the inotify backstop noticed (or forever, on a backend with none). Each
door now wakes the parks whose watched paths match what it changed: a move wakes for the source (gone) and the destination (new), and the event
type says what happened (``deleted`` / ``created`` / ``modified``).
"""

from __future__ import annotations

import httpx
import pytest

from primer.api.deps import get_event_bus, get_scheduler
from tests.api.test_workspace_state_reads_guarded import _app
from tests.bus.test_watch_wake_on_write import _make_watch_parked_session, _scheduler_with, _SpyBus

pytestmark = pytest.mark.asyncio


class _Ws:
    """A workspace that accepts every file mutation."""

    template = None

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def write_file(self, path, content):
        self.calls.append(("write", path))

    async def delete_file(self, path, *, recursive=False):
        self.calls.append(("delete", path))

    async def move_file(self, src, dst):
        self.calls.append(("move", src, dst))

    async def make_dir(self, path):
        self.calls.append(("mkdir", path))


async def _watching(paths: list[str]):
    sched = await _scheduler_with(_make_watch_parked_session(session_id="s1", tool_call_id="tc1", workspace_id="w", paths=paths))
    return sched, _SpyBus()


async def _rest(method: str, url: str, sched, bus, **kwargs):
    app, registry = _app("user")
    registry.ws = _Ws()
    app.dependency_overrides[get_scheduler] = lambda: sched
    app.dependency_overrides[get_event_bus] = lambda: bus
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        return await c.request(method, url, **kwargs)


def _changes(bus) -> list[tuple[str, str]]:
    """(event_type, path) of every wake published."""
    return [(c["event_type"], c["path"]) for _key, payload in bus.published for c in payload["changes"]]


async def test_a_rest_delete_wakes_a_watcher_of_the_path():
    sched, bus = await _watching(["notes.txt"])

    resp = await _rest("DELETE", "/v1/workspaces/w/files", sched, bus, params={"path": "notes.txt"})

    assert resp.status_code == 204, resp.text
    assert _changes(bus) == [("deleted", "notes.txt")]


async def test_a_rest_mkdir_wakes_a_watcher_of_the_directory():
    sched, bus = await _watching(["out"])

    resp = await _rest("POST", "/v1/workspaces/w/files/dir", sched, bus, params={"path": "out"})

    assert resp.status_code == 204, resp.text
    assert _changes(bus) == [("created", "out")]


async def test_a_rest_move_wakes_the_watchers_of_both_ends():
    sched, bus = await _watching(["a.txt", "b.txt"])

    resp = await _rest("POST", "/v1/workspaces/w/files/move", sched, bus, params={"src": "a.txt", "dst": "b.txt"})

    assert resp.status_code == 204, resp.text
    assert sorted(_changes(bus)) == [("created", "b.txt"), ("deleted", "a.txt")]


async def test_a_rest_write_still_wakes_with_modified():
    sched, bus = await _watching(["notes.txt"])

    resp = await _rest("PUT", "/v1/workspaces/w/files", sched, bus, params={"path": "notes.txt"}, json={"content": "x", "encoding": "text"})

    assert resp.status_code == 204, resp.text
    assert _changes(bus) == [("modified", "notes.txt")]


async def test_a_path_nobody_watches_wakes_nobody():
    sched, bus = await _watching(["other.txt"])

    await _rest("DELETE", "/v1/workspaces/w/files", sched, bus, params={"path": "notes.txt"})

    assert bus.published == []


async def test_a_refused_delete_wakes_nobody():
    """A mutation that fails changed nothing: no wake."""
    from primer.model.except_ import NotFoundError

    class _Missing(_Ws):
        async def delete_file(self, path, *, recursive=False):
            raise NotFoundError(f"{path!r} not found")

    sched, bus = await _watching(["notes.txt"])
    app, registry = _app("user")
    registry.ws = _Missing()
    app.dependency_overrides[get_scheduler] = lambda: sched
    app.dependency_overrides[get_event_bus] = lambda: bus
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.delete("/v1/workspaces/w/files", params={"path": "notes.txt"})

    assert resp.status_code == 404 and bus.published == []


async def test_a_wake_failure_never_fails_the_mutation():
    class _Exploding(_SpyBus):
        async def publish(self, event_key, payload=None):
            raise RuntimeError("bus exploded")

    sched, _ = await _watching(["notes.txt"])

    resp = await _rest("DELETE", "/v1/workspaces/w/files", sched, _Exploding(), params={"path": "notes.txt"})

    assert resp.status_code == 204


# ---- the MCP tools ------------------------------------------------------------------------------------------------------------------------------


async def _tool(name: str, arguments: dict, sched, bus):
    from primer.toolset.workspaces import build_workspaces_toolset
    from tests.toolset.test_workspace_tools_access import U1, _ctx, _Registry, _world

    world = await _world()
    toolset = build_workspaces_toolset(
        storage_provider=world.sp, workspace_registry=_Registry(world), scheduler=sched, claim_engine=world.engine, event_bus=bus,
    )
    return await toolset.call(tool_name=name, arguments=arguments, ctx=_ctx("ses-a", "ws-1", U1))


async def _watching_ws1(paths: list[str]):
    sched = await _scheduler_with(_make_watch_parked_session(session_id="s1", tool_call_id="tc1", workspace_id="ws-1", paths=paths))
    return sched, _SpyBus()


async def test_the_write_tool_wakes_a_watcher_of_the_path():
    sched, bus = await _watching_ws1(["notes.txt"])

    result = await _tool("write_workspace_file", {"workspace_id": "ws-1", "path": "notes.txt", "content": "x"}, sched, bus)

    assert not result.is_error, result.output
    assert _changes(bus) == [("modified", "notes.txt")]


async def test_the_delete_tool_wakes_a_watcher_of_the_path():
    sched, bus = await _watching_ws1(["notes.txt"])

    result = await _tool("delete_workspace_file", {"workspace_id": "ws-1", "path": "notes.txt"}, sched, bus)

    assert not result.is_error, result.output
    assert _changes(bus) == [("deleted", "notes.txt")]


async def test_the_tools_work_without_a_scheduler_and_wake_nobody_in_another_workspace():
    from primer.toolset.workspaces import build_workspaces_toolset
    from tests.toolset.test_workspace_tools_access import U1, _ctx, _Registry, _world

    world = await _world()
    bare = build_workspaces_toolset(storage_provider=world.sp, workspace_registry=_Registry(world), claim_engine=world.engine)
    result = await bare.call(tool_name="write_workspace_file", arguments={"workspace_id": "ws-1", "path": "notes.txt", "content": "x"}, ctx=_ctx("ses-a", "ws-1", U1))
    assert not result.is_error, result.output

    sched, bus = await _watching(["notes.txt"])  # the park is in workspace "w"; the write is in ws-1
    result = await _tool("write_workspace_file", {"workspace_id": "ws-1", "path": "notes.txt", "content": "x"}, sched, bus)
    assert not result.is_error and bus.published == []
