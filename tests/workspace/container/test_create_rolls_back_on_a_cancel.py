"""A container ``create`` that is cancelled, times out or fails after the container exists removes the container and its volume.

The rollback was ``except Exception``: a ``CancelledError`` or a caller's ``asyncio.timeout`` (BaseExceptions) that landed in the
file writes, an init command, ``materialise`` or the wait for the cache lock left the container running and its volume behind
with nothing pointing at them, and nothing else reclaims them (there is no orphan sweep, and a caller that did not pin the
workspace id never learns the generated one). The rollback now runs on its own task (``roll_back_shielded``): a second cancel does
not abandon it, the caller waits for it at most ``_ROLLBACK_WAIT_S``, and a step that fails is logged and neither masks the error
that ended the build nor skips the next step.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from primer.model.except_ import ConfigError
from primer.model.workspace import ContainerTemplateConfig, WorkspaceTemplate
from primer.workspace import base_backend
from primer.workspace.container.backend import ContainerWorkspaceBackend
from primer.workspace.sandbox.workspace import SandboxWorkspace
from tests.workspace.container.test_container_backend import _config
from tests.workspace.container.test_teardown_closes_its_connection import _Adapter, _Sandbox, _until
from tests.workspace.container.test_reattach_closes_its_sandbox import WORKSPACE_ID

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git CLI not available on PATH (SandboxStateRepo needs it)",
)


class _StuckSandbox(_Sandbox):
    """A sandbox whose ``exec`` (the init command) never answers while ``exec_gate`` is unset."""

    def __init__(self, root: Path, *, exec_gate: asyncio.Event | None = None, **kwargs) -> None:
        super().__init__(root, **kwargs)
        self.exec_gate = exec_gate
        self.exec_started = 0

    async def exec(self, command, **kwargs):
        self.exec_started += 1
        if self.exec_gate is not None:
            await self.exec_gate.wait()
        return await super().exec(command, **kwargs)


class _StuckAdapter(_Adapter):
    def _new(self, name: str) -> _StuckSandbox:
        root = self._tmp / name
        root.mkdir(parents=True, exist_ok=True)
        sandbox = _StuckSandbox(root, **self._sandbox_kwargs)
        self.handed_out.append(sandbox)
        return sandbox


def _template(*init_commands: str) -> WorkspaceTemplate:
    return WorkspaceTemplate(
        id="t1", provider_id="c1", description="", backend=ContainerTemplateConfig(image="alpine:latest"),
        init_commands=list(init_commands),
    )


async def _backend(tmp_path: Path, **sandbox_kwargs):
    adapter = _StuckAdapter(tmp_path, **sandbox_kwargs)
    backend = ContainerWorkspaceBackend(_config(), adapter=adapter)
    await backend.initialize()
    return backend, adapter


async def _stuck_create(tmp_path: Path, **sandbox_kwargs):
    """A ``create`` that is in its init command, which never answers."""
    backend, adapter = await _backend(tmp_path, exec_gate=asyncio.Event(), **sandbox_kwargs)
    task = asyncio.create_task(backend.create(_template("echo hi"), workspace_id=WORKSPACE_ID))
    await _until(lambda: adapter.handed_out and adapter.handed_out[0].exec_started, "the init command started")
    return backend, adapter, task


def _rolled_back(backend, adapter) -> bool:
    (sandbox,) = adapter.handed_out
    return sandbox.removed == 1 and sandbox.closed == 1 and adapter._volumes == set() and backend._workspaces == {}


async def test_a_cancel_in_an_init_command_removes_the_container_and_its_volume_and_closes_the_connection(tmp_path):
    backend, adapter, task = await _stuck_create(tmp_path)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert _rolled_back(backend, adapter)


async def test_a_timeout_in_an_init_command_is_rolled_back_too(tmp_path):
    backend, adapter = await _backend(tmp_path, exec_gate=asyncio.Event())
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            await backend.create(_template("echo hi"), workspace_id=WORKSPACE_ID)
    assert _rolled_back(backend, adapter)


async def test_a_cancel_while_waiting_for_the_cache_lock_is_rolled_back_too(tmp_path, monkeypatch):
    """The workspace is built but not cached yet: the container is running and nothing points at it."""
    backend, adapter = await _backend(tmp_path)
    built = asyncio.Event()
    real = SandboxWorkspace.materialise.__func__

    async def materialise_then_signal(**kwargs):
        ws = await real(SandboxWorkspace, **kwargs)
        built.set()
        return ws

    monkeypatch.setattr(SandboxWorkspace, "materialise", staticmethod(materialise_then_signal))
    await backend._lock.acquire()  # the cache lock is held by someone else
    try:
        task = asyncio.create_task(backend.create(_template(), workspace_id=WORKSPACE_ID))
        await asyncio.wait_for(built.wait(), timeout=5)
        await asyncio.sleep(0.05)  # create is now waiting for the lock
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
    finally:
        backend._lock.release()
    assert _rolled_back(backend, adapter)


async def test_a_second_cancel_during_the_rollback_does_not_abandon_it(tmp_path):
    remove_gate = asyncio.Event()
    backend, adapter, task = await _stuck_create(tmp_path, remove_gate=remove_gate)
    try:
        task.cancel()
        await _until(lambda: adapter.handed_out[0].remove_started, "the rollback reached the container removal")
        task.cancel()  # a drain, a second bound
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
    finally:
        remove_gate.set()
    await _until(lambda: _rolled_back(backend, adapter), "the rollback finished after the second cancel")


async def test_a_rollback_that_hangs_does_not_hold_the_caller_past_its_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(base_backend, "_ROLLBACK_WAIT_S", 0.05)
    remove_gate = asyncio.Event()
    backend, adapter = await _backend(tmp_path, remove_gate=remove_gate)
    try:
        with pytest.raises(ConfigError, match="init command failed"):
            await asyncio.wait_for(backend.create(_template("false"), workspace_id=WORKSPACE_ID), timeout=2)
        (sandbox,) = adapter.handed_out
        assert sandbox.remove_started == 1 and sandbox.removed == 0
    finally:
        remove_gate.set()
    await _until(lambda: _rolled_back(backend, adapter), "the rollback finished in the background")


async def test_a_failing_rollback_step_neither_masks_the_error_nor_skips_the_next_one(tmp_path):
    backend, adapter = await _backend(tmp_path, remove_error=RuntimeError("the daemon refused"))
    with pytest.raises(ConfigError, match="init command failed"):
        await backend.create(_template("false"), workspace_id=WORKSPACE_ID)
    (sandbox,) = adapter.handed_out
    assert sandbox.closed == 1 and adapter._volumes == set()  # the connection was closed and the volume removed regardless


async def test_a_create_that_finishes_is_not_rolled_back(tmp_path):
    backend, adapter = await _backend(tmp_path)
    ws = await backend.create(_template(), workspace_id=WORKSPACE_ID)
    (sandbox,) = adapter.handed_out
    assert backend._workspaces == {WORKSPACE_ID: ws}
    assert sandbox.removed == 0 and sandbox.close_started == 0 and len(adapter._volumes) == 1
