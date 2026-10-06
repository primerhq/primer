"""The container backend closes the connection a teardown is done with, and no teardown close waits without a bound.

``stop()`` and ``remove()`` only reach the container daemon. The connection (the sandbox's ``RuntimeClient``: an aiohttp
session and a WebSocket that reconnects on every drop and gives up only on a 404 handshake) outlives them unless something
closes it, and a workspace's own ``aclose`` only ends its sessions. ``destroy`` closed nothing: not the workspace's own
sandbox, not the new one ``get_sandbox`` hands out on every call, so the client of a container that no longer exists kept
reconnecting to it. Two older closes waited without a bound for a peer that may be silent: the ``create`` rollback (in front
of the container's removal) and the eviction of a gone cached handle (``aclose`` ends the handle's sessions, each a state
commit on a connection that is gone, and a request on a disconnected client waits for a reconnect that never comes).
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from primer.model.except_ import ConfigError
from primer.model.workspace import ContainerTemplateConfig, WorkspaceTemplate
from primer.model.workspace_session import AgentBinding
from primer.workspace import base_backend
from primer.workspace.container.backend import ContainerWorkspaceBackend
from tests.workspace.container.test_container_backend import _config, _FakeAdapter, _template
from tests.workspace.container.test_reattach_closes_its_sandbox import WORKSPACE_ID, _CountingSandbox

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git CLI not available on PATH (SandboxStateRepo needs it)",
)


class _Sandbox(_CountingSandbox):
    """A sandbox with a connection of its own to count the close of, and a container to count the teardown of."""

    def __init__(self, root: Path, *, remove_gate: asyncio.Event | None = None, remove_error: Exception | None = None,
                 **kwargs) -> None:
        super().__init__(root, **kwargs)
        self.remove_gate, self.remove_error = remove_gate, remove_error
        self.remove_started = 0
        self.removed = 0

    async def remove(self) -> None:
        self.remove_started += 1
        if self.remove_gate is not None:
            await self.remove_gate.wait()
        if self.remove_error is not None:
            raise self.remove_error
        self.removed += 1
        await super().remove()


class _Adapter(_FakeAdapter):
    """Like the real adapter: ``create_sandbox`` and every ``get_sandbox`` hand out a NEW sandbox with its own connection."""

    def __init__(self, tmp_path: Path, **sandbox_kwargs) -> None:
        super().__init__(tmp_path)
        self.handed_out: list[_Sandbox] = []
        self._sandbox_kwargs = sandbox_kwargs

    def _new(self, name: str) -> _Sandbox:
        root = self._tmp / name
        root.mkdir(parents=True, exist_ok=True)
        sandbox = _Sandbox(root, **self._sandbox_kwargs)
        self.handed_out.append(sandbox)
        return sandbox

    async def create_sandbox(self, *, name, volume_name, **kwargs):
        self._volumes.add(volume_name)
        return self._new(name)

    async def get_sandbox(self, name: str):
        return self._new(name)


async def _backend(tmp_path: Path, **sandbox_kwargs):
    adapter = _Adapter(tmp_path, **sandbox_kwargs)
    backend = ContainerWorkspaceBackend(_config(), adapter=adapter)
    await backend.initialize()
    return backend, adapter


async def _until(condition, what: str) -> None:
    for _ in range(500):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"never happened: {what}")


# ---- destroy -----------------------------------------------------------------------------------------------------------------------

async def test_destroy_closes_the_connection_of_a_cached_workspace(tmp_path):
    backend, adapter = await _backend(tmp_path)
    await backend.create(_template(), workspace_id=WORKSPACE_ID)
    (sandbox,) = adapter.handed_out
    await backend.destroy(WORKSPACE_ID)
    assert sandbox.removed == 1 and sandbox.closed == 1
    assert adapter._volumes == set() and backend._workspaces == {}


async def test_destroy_closes_the_connection_get_sandbox_handed_it_when_nothing_is_cached(tmp_path):
    backend, adapter = await _backend(tmp_path)
    await backend.destroy(WORKSPACE_ID)
    (sandbox,) = adapter.handed_out
    assert sandbox.removed == 1 and sandbox.closed == 1


async def test_destroy_closes_the_connection_when_the_teardown_fails(tmp_path):
    backend, adapter = await _backend(tmp_path, remove_error=RuntimeError("the daemon is down"))
    with pytest.raises(RuntimeError, match="the daemon is down"):
        await backend.destroy(WORKSPACE_ID)
    (sandbox,) = adapter.handed_out
    assert sandbox.closed == 1


async def test_destroy_closes_the_connection_when_it_is_cancelled(tmp_path):
    remove_gate = asyncio.Event()
    backend, adapter = await _backend(tmp_path, remove_gate=remove_gate)
    task = asyncio.create_task(backend.destroy(WORKSPACE_ID))
    try:
        await _until(lambda: adapter.handed_out and adapter.handed_out[0].remove_started, "the removal started")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
    finally:
        remove_gate.set()
    (sandbox,) = adapter.handed_out
    assert sandbox.closed == 1


async def test_destroy_does_not_wait_out_a_close_that_hangs(tmp_path, monkeypatch):
    monkeypatch.setattr(base_backend, "_CLOSE_WAIT_S", 0.05)
    close_gate = asyncio.Event()
    backend, adapter = await _backend(tmp_path, close_gate=close_gate)
    try:
        await asyncio.wait_for(backend.destroy(WORKSPACE_ID), timeout=2)
        (sandbox,) = adapter.handed_out
        assert sandbox.removed == 1 and sandbox.close_started == 1 and sandbox.closed == 0
    finally:
        close_gate.set()
        await asyncio.sleep(0.05)


# ---- the create rollback ---------------------------------------------------------------------------------------------------------

async def test_the_create_rollback_removes_the_container_without_waiting_out_a_close_that_hangs(tmp_path, monkeypatch):
    monkeypatch.setattr(base_backend, "_CLOSE_WAIT_S", 0.05)
    close_gate = asyncio.Event()
    backend, adapter = await _backend(tmp_path, close_gate=close_gate)
    failing = WorkspaceTemplate(
        id="t1", provider_id="c1", description="", backend=ContainerTemplateConfig(image="alpine:latest"),
        init_commands=["false"],
    )
    try:
        with pytest.raises(ConfigError, match="init command failed"):
            await asyncio.wait_for(backend.create(failing, workspace_id=WORKSPACE_ID), timeout=2)
        (sandbox,) = adapter.handed_out
        assert sandbox.close_started == 1 and sandbox.closed == 0
        assert sandbox.removed == 1 and adapter._volumes == set() and backend._workspaces == {}
    finally:
        close_gate.set()
        await asyncio.sleep(0.05)


async def test_the_create_rollback_closes_the_connection_and_removes_the_container(tmp_path):
    backend, adapter = await _backend(tmp_path)
    failing = WorkspaceTemplate(
        id="t1", provider_id="c1", description="", backend=ContainerTemplateConfig(image="alpine:latest"),
        init_commands=["false"],
    )
    with pytest.raises(ConfigError, match="init command failed"):
        await backend.create(failing, workspace_id=WORKSPACE_ID)
    (sandbox,) = adapter.handed_out
    assert sandbox.closed == 1 and sandbox.removed == 1


# ---- the eviction of a gone cached handle ----------------------------------------------------------------------------------------

async def test_get_does_not_wait_out_the_session_ends_of_a_gone_handle(tmp_path, monkeypatch):
    """The handle's connection is gone, so ending its live session (a state commit on it) never answers; the eviction must
    not hold ``get`` for it, and the workspace is re-attached over a fresh connection meanwhile."""
    monkeypatch.setattr(base_backend, "_CLOSE_WAIT_S", 0.05)
    backend, adapter = await _backend(tmp_path)
    ws = await backend.create(_template(), workspace_id=WORKSPACE_ID)
    await ws.start_session(AgentBinding(agent_id="agent-foo", agent_name="Agent Foo"))
    commit_gate, commit_entered = asyncio.Event(), asyncio.Event()

    async def state_commit_on_a_gone_connection(**kwargs):
        commit_entered.set()
        await commit_gate.wait()

    ws.sandbox.state_commit = state_commit_on_a_gone_connection
    ws.sandbox.gone = True
    try:
        fresh = await asyncio.wait_for(backend.get(WORKSPACE_ID, template=_template()), timeout=2)
        assert commit_entered.is_set(), "the eviction really did wait on the gone connection"
        assert fresh is not None and fresh is not ws and backend._workspaces == {WORKSPACE_ID: fresh}
    finally:
        commit_gate.set()
        await asyncio.sleep(0.05)
