"""The container backend closes the sandbox it re-attached to when the re-attach does not finish.

``adapter.get_sandbox`` returns a FRESH sandbox with its own connected ``RuntimeClient`` on every call (the Docker adapter
reconnects each time), so a sandbox that ``_reattach`` does not cache is held by nothing else. It was leaked on every exit
but the cached one: no template (it returned ``None`` with the connection open), a template of the wrong kind, a failure
or a cancel or a caller's timeout in ``materialise`` or while waiting for the cache lock, and the race it lost (the comment
there said "it's the same one", which it is not: the same container, another connection). The relay's bounded read reaches
this through ``registry.get_workspace -> backend.get -> _reattach`` on a cache miss.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

import pytest

from primer.int.sandbox import Sandbox
from primer.model.workspace import KubernetesTemplateConfig, WorkspaceTemplate
from primer.workspace.container.backend import ContainerWorkspaceBackend
from primer.workspace.sandbox.fake import FakeSandbox
from primer.workspace.sandbox.workspace import SandboxWorkspace
from tests.workspace.container.test_container_backend import _config, _FakeAdapter, _template

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git CLI not available on PATH (SandboxStateRepo needs it)",
)

WORKSPACE_ID = "ws-1"
NAME = f"workspace-{WORKSPACE_ID}"


class _CountingSandbox(FakeSandbox):
    """A sandbox with its own connection to count the close of."""

    def __init__(self, root: Path, *, close_gate: asyncio.Event | None = None, close_error: Exception | None = None) -> None:
        super().__init__(root=root, sandbox_id=NAME)
        self.close_gate, self.close_error = close_gate, close_error
        self.close_started = 0
        self.closed = 0
        self.mapped_host_port = 32100

    async def aclose(self) -> None:
        self.close_started += 1
        if self.close_gate is not None:
            await self.close_gate.wait()
        if self.close_error is not None:
            raise self.close_error
        self.closed += 1


class _ReconnectingAdapter(_FakeAdapter):
    """Like the real adapter: every ``get_sandbox`` hands out a NEW sandbox (a new connection to the same container)."""

    def __init__(self, tmp_path: Path, **sandbox_kwargs) -> None:
        super().__init__(tmp_path)
        self.handed_out: list[_CountingSandbox] = []
        self._sandbox_kwargs = sandbox_kwargs

    async def get_sandbox(self, name: str) -> Sandbox | None:
        root = self._tmp / name
        root.mkdir(parents=True, exist_ok=True)
        sandbox = _CountingSandbox(root, **self._sandbox_kwargs)
        self.handed_out.append(sandbox)
        return sandbox


async def _backend(tmp_path: Path, **sandbox_kwargs):
    adapter = _ReconnectingAdapter(tmp_path, **sandbox_kwargs)
    backend = ContainerWorkspaceBackend(_config(), adapter=adapter)
    await backend.initialize()
    return backend, adapter


def _blocks(monkeypatch) -> asyncio.Event:
    entered = asyncio.Event()

    async def blocked(**kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(SandboxWorkspace, "materialise", staticmethod(blocked))
    return entered


async def test_no_template_returns_none_and_closes_the_sandbox_it_connected(tmp_path):
    backend, adapter = await _backend(tmp_path)
    assert await backend.get(WORKSPACE_ID, template=None) is None
    (sandbox,) = adapter.handed_out
    assert sandbox.closed == 1 and backend._workspaces == {}


async def test_a_template_of_the_wrong_kind_raises_and_closes_the_sandbox(tmp_path):
    from primer.model.except_ import ConfigError

    backend, adapter = await _backend(tmp_path)
    wrong = WorkspaceTemplate(id="t2", provider_id="c1", description="", backend=KubernetesTemplateConfig(image="x:1"))
    with pytest.raises(ConfigError):
        await backend.get(WORKSPACE_ID, template=wrong)
    (sandbox,) = adapter.handed_out
    assert sandbox.closed == 1 and backend._workspaces == {}


async def test_a_materialise_that_raises_closes_the_sandbox(tmp_path, monkeypatch):
    async def failing(**kwargs):
        raise RuntimeError("the sandbox could not be wrapped")

    monkeypatch.setattr(SandboxWorkspace, "materialise", staticmethod(failing))
    backend, adapter = await _backend(tmp_path)
    with pytest.raises(RuntimeError, match="could not be wrapped"):
        await backend.get(WORKSPACE_ID, template=_template())
    (sandbox,) = adapter.handed_out
    assert sandbox.closed == 1 and backend._workspaces == {}


async def test_a_cancel_during_materialise_closes_the_sandbox(tmp_path, monkeypatch):
    entered = _blocks(monkeypatch)
    backend, adapter = await _backend(tmp_path)
    task = asyncio.create_task(backend.get(WORKSPACE_ID, template=_template()))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (sandbox,) = adapter.handed_out
    assert sandbox.closed == 1 and backend._workspaces == {}


async def test_a_timeout_during_materialise_closes_the_sandbox(tmp_path, monkeypatch):
    """The relay's shape: a caller's ``asyncio.timeout`` expiring while the workspace is being rebuilt."""
    _blocks(monkeypatch)
    backend, adapter = await _backend(tmp_path)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            await backend.get(WORKSPACE_ID, template=_template())
    (sandbox,) = adapter.handed_out
    assert sandbox.closed == 1 and backend._workspaces == {}


async def test_a_cancel_while_waiting_for_the_cache_lock_closes_the_sandbox(tmp_path, monkeypatch):
    backend, adapter = await _backend(tmp_path)
    wrapped = asyncio.Event()
    real = SandboxWorkspace.materialise.__func__

    async def materialise_then_hold_the_lock(**kwargs):
        ws = await real(SandboxWorkspace, **kwargs)
        await backend._lock.acquire()           # ``get`` takes the lock earlier too, to look in the cache
        wrapped.set()
        return ws

    monkeypatch.setattr(SandboxWorkspace, "materialise", staticmethod(materialise_then_hold_the_lock))
    task = asyncio.create_task(backend.get(WORKSPACE_ID, template=_template()))
    try:
        await asyncio.wait_for(wrapped.wait(), timeout=5)
        await asyncio.sleep(0.05)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if backend._lock.locked():
            backend._lock.release()
    (sandbox,) = adapter.handed_out
    assert sandbox.closed == 1 and backend._workspaces == {}


async def test_a_re_attach_that_finishes_keeps_its_sandbox_open_and_cached(tmp_path):
    backend, adapter = await _backend(tmp_path)
    ws = await backend.get(WORKSPACE_ID, template=_template())
    (sandbox,) = adapter.handed_out
    assert isinstance(ws, SandboxWorkspace) and backend._workspaces == {WORKSPACE_ID: ws}
    assert sandbox.closed == 0 and sandbox.close_started == 0


async def test_the_caller_that_lost_the_race_closes_its_own_sandbox_and_gets_the_winner(tmp_path, monkeypatch):
    """The container is the same, the connection is not: the loser's sandbox is closed (the container is left alone)."""
    winner = object()
    backend, adapter = await _backend(tmp_path)
    real = SandboxWorkspace.materialise.__func__

    async def materialise_while_another_caller_wins(**kwargs):
        ws = await real(SandboxWorkspace, **kwargs)
        backend._workspaces[WORKSPACE_ID] = winner
        return ws

    monkeypatch.setattr(SandboxWorkspace, "materialise", staticmethod(materialise_while_another_caller_wins))
    assert await backend.get(WORKSPACE_ID, template=_template()) is winner
    (sandbox,) = adapter.handed_out
    assert sandbox.closed == 1 and backend._workspaces == {WORKSPACE_ID: winner}


async def test_the_losers_close_runs_outside_the_cache_lock(tmp_path, monkeypatch):
    """While the race loser's sandbox is being closed (held on a gate) the cache lock is free: every get, create and destroy
    on the backend would otherwise wait behind a close that a silent peer can stretch."""
    close_gate = asyncio.Event()
    winner = object()
    backend, adapter = await _backend(tmp_path, close_gate=close_gate)
    real = SandboxWorkspace.materialise.__func__

    async def materialise_while_another_caller_wins(**kwargs):
        ws = await real(SandboxWorkspace, **kwargs)
        backend._workspaces[WORKSPACE_ID] = winner
        return ws

    monkeypatch.setattr(SandboxWorkspace, "materialise", staticmethod(materialise_while_another_caller_wins))
    task = asyncio.create_task(backend.get(WORKSPACE_ID, template=_template()))
    try:
        for _ in range(200):
            if adapter.handed_out and adapter.handed_out[0].close_started:
                break
            await asyncio.sleep(0.01)
        assert adapter.handed_out[0].close_started == 1 and not task.done()
        assert not backend._lock.locked(), "the loser's close runs outside the cache lock"
    except BaseException:
        # A failed check must end the test here, in seconds: nothing is left waiting on the gate or for the task.
        task.cancel()
        raise
    finally:
        close_gate.set()
    assert await asyncio.wait_for(task, timeout=5) is winner


async def test_two_concurrent_gets_on_a_cold_cache_share_one_workspace_and_only_the_loser_closes(tmp_path, monkeypatch):
    """A REAL race: two callers re-attach at once, each with a sandbox of its own (a new connection to the same container);
    materialise waits until both have arrived. Both get the same workspace; the cached sandbox, the shared connection, stays
    open, and only the other caller's is closed (a build that closed the winner's would pass a stand-in-winner test)."""
    backend, adapter = await _backend(tmp_path)
    arrived, both_here = 0, asyncio.Event()
    real = SandboxWorkspace.materialise.__func__

    async def wait_for_the_other_caller(**kwargs):
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            both_here.set()
        await both_here.wait()
        return await real(SandboxWorkspace, **kwargs)

    monkeypatch.setattr(SandboxWorkspace, "materialise", staticmethod(wait_for_the_other_caller))
    first, second = await asyncio.wait_for(
        asyncio.gather(backend.get(WORKSPACE_ID, template=_template()), backend.get(WORKSPACE_ID, template=_template())),
        timeout=10,
    )
    assert first is second and backend._workspaces == {WORKSPACE_ID: first}
    assert len(adapter.handed_out) == 2
    cached = first.sandbox
    (other,) = [sb for sb in adapter.handed_out if sb is not cached]
    assert cached.close_started == 0 and cached.closed == 0, "the cached connection is the shared one: it stays open"
    assert other.closed == 1, "the caller that lost the race closed its own"


async def test_a_close_that_fails_is_logged_and_does_not_replace_the_error(tmp_path, monkeypatch, caplog):
    async def failing(**kwargs):
        raise RuntimeError("the sandbox could not be wrapped")

    monkeypatch.setattr(SandboxWorkspace, "materialise", staticmethod(failing))
    backend, adapter = await _backend(tmp_path, close_error=OSError("the socket was already gone"))
    with caplog.at_level(logging.WARNING, logger="primer.workspace"):
        with pytest.raises(RuntimeError, match="could not be wrapped"):
            await backend.get(WORKSPACE_ID, template=_template())
    assert adapter.handed_out[0].close_started == 1
    assert any("aclose failed" in r.getMessage() for r in caplog.records)


async def test_a_second_cancel_does_not_leave_the_close_half_done(tmp_path, monkeypatch):
    close_gate = asyncio.Event()
    entered = _blocks(monkeypatch)
    backend, adapter = await _backend(tmp_path, close_gate=close_gate)
    task = asyncio.create_task(backend.get(WORKSPACE_ID, template=_template()))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    sandbox = adapter.handed_out[0]
    for _ in range(100):
        if sandbox.close_started:
            break
        await asyncio.sleep(0.01)
    assert sandbox.close_started == 1 and not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sandbox.closed == 0
    close_gate.set()
    for _ in range(100):
        if sandbox.closed:
            break
        await asyncio.sleep(0.01)
    assert sandbox.closed == 1
