"""A destroy and a get of the same container workspace no longer restart the container under each other (X3.3, the
race widened by the 5.6 fix: a re-attach now starts a stopped container, and a destroy of the same id races it).

Before the fix, ``destroy`` popped the cached handle and then tore the container down while a concurrent ``get``
could run the re-attach: the re-attach STARTS the container (the 5.6/W21 behaviour) and caches a fresh handle after
the destroy already popped, so the caller holds a handle whose container is seconds from being removed, and the
handle leaks in the cache (the destroy will not evict what it already popped). The backend now fences a
per-workspace destroy from its pop to its teardown end: a re-attach of a fenced id refuses to start the container
and to cache a handle (it answers ``None``; the row the registry deletes makes the id 404 to everyone else).

No Docker daemon: the aiodocker client is a fake and the reconnect/wrapper build are monkeypatched, as in
``test_stopped_container_evicted_on_get.py`` (the fakes come from there).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from primer.workspace.runtime import docker as docker_mod
from primer.workspace.sandbox.workspace import SandboxWorkspace

from tests.workspace.container.test_stopped_container_evicted_on_get import (
    _backend,
    _cached_handle,
    _docker_with_container,
    _template,
)


async def _poll_until(what: str, condition) -> None:
    """Wait (cooperatively) for *condition*; fail with a named error instead of spinning forever on a hang."""
    for _ in range(1000):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError(f"timed out waiting for: {what}")


async def test_a_get_while_a_destroy_is_running_does_not_restart_the_container(monkeypatch) -> None:
    """Destroy holds the fence from the cache pop to the teardown end: a get in that window must not run the
    re-attach (which would start the container and hand out a handle destroy is about to remove)."""
    docker = _docker_with_container("exited")
    backend = _backend(docker)
    old = _cached_handle(backend, "ws-1")
    old.sandbox.stop = AsyncMock()
    old.sandbox.remove = AsyncMock()
    started = asyncio.Event()
    release = asyncio.Event()

    async def _stop_gate():
        started.set()
        await release.wait()

    old.sandbox.stop = _stop_gate  # pause destroy mid-teardown, fence already set
    monkeypatch.setattr(backend._adapter, "remove_volume", AsyncMock())

    # The re-attach leaf calls, so the pre-fix run fails on an assertion, not on a real WebSocket.
    new_handle = MagicMock()
    new_handle.gone = False

    async def _fake_make(docker_, cont, name, token, *, reachability, workspace_root):
        return MagicMock()

    async def _fake_materialise(cls, **kwargs):
        return new_handle

    monkeypatch.setattr(docker_mod, "_make_ws_sandbox", _fake_make)
    monkeypatch.setattr(SandboxWorkspace, "materialise", classmethod(_fake_materialise))

    destroy_task = asyncio.create_task(backend.destroy("ws-1"))
    await started.wait()  # destroy is past the pop (and the fence, post-fix), inside the teardown

    assert await backend.get("ws-1", template=_template()) is None
    docker.containers.get.return_value.start.assert_not_awaited()  # the container is NOT restarted
    assert "ws-1" not in backend._workspaces

    release.set()
    await destroy_task


async def test_a_reattach_in_flight_when_destroy_starts_does_not_cache_its_handle(monkeypatch) -> None:
    """The post-build re-check: a re-attach that passed the fence check before the destroy started must not cache
    the handle once the destroy owns the id (it closes the sandbox it opened and answers ``None``)."""
    docker = _docker_with_container("exited")
    backend = _backend(docker)
    gate = asyncio.Event()
    proceed = asyncio.Event()
    new_handle = MagicMock()
    new_handle.gone = False
    new_sandbox = MagicMock()
    new_sandbox.mapped_host_port = 5960
    new_sandbox.workspace_root = "/workspace"
    new_sandbox.aclose = AsyncMock()
    new_sandbox.stop = AsyncMock()
    new_sandbox.remove = AsyncMock()
    monkeypatch.setattr(backend._adapter, "remove_volume", AsyncMock())

    async def _fake_make(docker_, cont, name, token, *, reachability, workspace_root):
        return new_sandbox

    async def _fake_materialise(cls, **kwargs):
        gate.set()  # the re-attach is past the fence check, inside the wrapper build
        await proceed.wait()
        return new_handle

    monkeypatch.setattr(docker_mod, "_make_ws_sandbox", _fake_make)
    monkeypatch.setattr(SandboxWorkspace, "materialise", classmethod(_fake_materialise))

    get_sandbox_calls = 0

    orig_get_sandbox = backend._adapter.get_sandbox

    async def _spy(name):
        nonlocal get_sandbox_calls
        get_sandbox_calls += 1
        return await orig_get_sandbox(name)

    monkeypatch.setattr(backend._adapter, "get_sandbox", _spy)

    get_task = asyncio.create_task(backend.get("ws-1", template=_template()))
    await gate.wait()  # call #1 (the re-attach) is done; the re-attach is held in the build

    destroy_task = asyncio.create_task(backend.destroy("ws-1"))
    # Destroy pops the cache FIRST, then reaches its own get_sandbox (call #2): from its pop on it owns the id.
    await _poll_until("destroy to reach its get_sandbox (call #2)", lambda: get_sandbox_calls >= 2)

    proceed.set()
    assert await get_task is None, "a handle built under a running destroy is not handed out"
    assert "ws-1" not in backend._workspaces, "... nor cached (the destroy already popped the cache)"
    assert new_sandbox.aclose.await_count >= 1  # the re-attach's own connection is closed, not leaked
    await destroy_task


async def test_a_destroy_of_an_absent_container_still_removes_the_volume(monkeypatch) -> None:
    """The fence does not change the no-cache destroy path: a container that is already gone (404) still has its
    volume removed, or a same-id create reuses it and the old data comes back (review W19)."""
    docker = _docker_with_container(Exception("404 container workspace-ws-1 not found"))
    backend = _backend(docker)
    removed = []

    async def _fake_remove_by_name(name, volume_name):
        removed.append((name, volume_name))
        return True

    monkeypatch.setattr(backend._adapter, "remove_by_name", _fake_remove_by_name)

    await backend.destroy("ws-1")

    assert removed == [("workspace-ws-1", "workspace-ws-1-data")]
    assert "ws-1" not in backend._workspaces
