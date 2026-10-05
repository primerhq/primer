"""The k8s backend closes the runtime client it opened when ``create`` or ``_reattach`` does not finish.

Both build a ``RuntimeClient``, ``await client.connect()`` (a WebSocket and an ``aiohttp`` session), then wrap it
(``SandboxWorkspace.materialise``) and put the workspace in the cache. Anything that ends the build in between left the
connection open and uncached: ``create`` closed it only for an ``Exception`` (a cancel or a timeout is a ``BaseException``)
and only from the sandbox set-up on, and ``_reattach`` closed it only when another caller won the race. The relay's bounded
read (``asyncio.timeout``) goes through ``registry.get_workspace -> backend.get -> _reattach`` on a cache miss, so a slow
materialise on a cache-cold worker now cancels it, and the next call connects (and leaks) again.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from primer.model.workspace import (
    K8sConnectionInCluster,
    K8sReachabilityInCluster,
    KubernetesTemplateConfig,
    KubernetesWorkspaceConfig,
    WorkspaceTemplate,
)
from primer.workspace.k8s import backend as k8s_backend
from primer.workspace.k8s.backend import KubernetesWorkspaceBackend
from primer.workspace.sandbox.workspace import SandboxWorkspace

TEMPLATE = WorkspaceTemplate(
    id="tpl-1", provider_id="prov-1", description="", backend=KubernetesTemplateConfig(image="primer-runtime:1"),
)


class _Client:
    """A ``RuntimeClient`` that records what happens to it."""

    def __init__(self, *, connect_gate: asyncio.Event | None = None, close_gate: asyncio.Event | None = None,
                 close_error: Exception | None = None) -> None:
        self.connect_gate, self.close_gate, self.close_error = connect_gate, close_gate, close_error
        self.connect_started = asyncio.Event()
        self.connected = False
        self.close_started = 0
        self.closed = 0

    async def connect(self) -> None:
        self.connect_started.set()
        if self.connect_gate is not None:
            await self.connect_gate.wait()
        self.connected = True

    async def aclose(self) -> None:
        self.close_started += 1
        if self.close_gate is not None:
            await self.close_gate.wait()
        if self.close_error is not None:
            raise self.close_error
        self.closed += 1


def _backend() -> KubernetesWorkspaceBackend:
    cfg = KubernetesWorkspaceConfig(
        connection=K8sConnectionInCluster(), namespace="primer-ns", reachability=K8sReachabilityInCluster(),
    )
    backend = KubernetesWorkspaceBackend.__new__(KubernetesWorkspaceBackend)
    backend._config = cfg
    backend._core_v1 = AsyncMock()
    backend._apps_v1 = AsyncMock()
    backend._workspaces = {}
    backend._lock = asyncio.Lock()
    backend._initialised = True
    backend._wait_for_pod_running = AsyncMock()
    backend._apps_v1.read_namespaced_stateful_set = AsyncMock()
    backend._core_v1.read_namespaced_secret = AsyncMock(
        return_value=SimpleNamespace(data=None, string_data={"RUNTIME_TOKEN": "the-stored-token"}),
    )
    backend._core_v1.create_namespaced_secret = AsyncMock()
    backend._core_v1.create_namespaced_service = AsyncMock()
    backend._apps_v1.create_namespaced_stateful_set = AsyncMock()
    return backend


@pytest.fixture
def client(monkeypatch) -> _Client:
    c = _Client()
    monkeypatch.setattr(k8s_backend, "RuntimeClient", lambda **kwargs: c)
    return c


def _materialise_blocks(monkeypatch) -> asyncio.Event:
    """``SandboxWorkspace.materialise`` waits for the returned event to be set; it signals ``entered`` first."""
    entered = asyncio.Event()

    async def blocked(**kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(SandboxWorkspace, "materialise", staticmethod(blocked))
    return entered


def _materialise_raises(monkeypatch, exc: Exception) -> None:
    async def failing(**kwargs):
        raise exc

    monkeypatch.setattr(SandboxWorkspace, "materialise", staticmethod(failing))


async def _reattach(backend: KubernetesWorkspaceBackend):
    return await backend.get("ws-1", template=TEMPLATE)


async def _create(backend: KubernetesWorkspaceBackend):
    return await backend.create(TEMPLATE, overrides=None, workspace_id="ws-1")


BUILDS = pytest.mark.parametrize("build", [_reattach, _create], ids=["reattach", "create"])


# ---- the build that ends without a workspace --------------------------------------------------------------------------------------

@BUILDS
async def test_a_materialise_that_raises_closes_the_client_and_caches_nothing(monkeypatch, client, build):
    _materialise_raises(monkeypatch, RuntimeError("the sandbox could not be wrapped"))
    backend = _backend()
    with pytest.raises(RuntimeError, match="could not be wrapped"):
        await build(backend)
    assert client.connected and client.closed == 1 and backend._workspaces == {}


@BUILDS
async def test_a_cancel_between_connect_and_the_cache_closes_the_client(monkeypatch, client, build):
    entered = _materialise_blocks(monkeypatch)
    backend = _backend()
    task = asyncio.create_task(build(backend))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.connected and client.closed == 1 and backend._workspaces == {}


@BUILDS
async def test_a_timeout_between_connect_and_the_cache_closes_the_client(monkeypatch, client, build):
    """The shape the relay produces: a caller's ``asyncio.timeout`` expiring while the workspace is being built."""
    _materialise_blocks(monkeypatch)
    backend = _backend()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            await build(backend)
    assert client.connected and client.closed == 1 and backend._workspaces == {}


@BUILDS
async def test_a_cancel_during_connect_itself_closes_the_client(monkeypatch, build):
    """``RuntimeClient.connect`` opens an aiohttp session before the handshake: cancelled in the middle, the client holds a
    session and maybe a socket that only ``aclose`` releases."""
    c = _Client(connect_gate=asyncio.Event())
    monkeypatch.setattr(k8s_backend, "RuntimeClient", lambda **kwargs: c)
    backend = _backend()
    task = asyncio.create_task(build(backend))
    await asyncio.wait_for(c.connect_started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not c.connected and c.closed == 1 and backend._workspaces == {}


@BUILDS
async def test_a_cancel_while_waiting_for_the_cache_lock_closes_the_client(monkeypatch, client, build):
    """The workspace is wrapped but the cache lock is held by someone else: cancelled there, the client is still ours.
    (The lock is taken by the stand-in for ``materialise`` on its way out, because ``get`` takes it too, earlier, to look
    in the cache.)"""
    backend = _backend()
    wrapped = asyncio.Event()
    real = SandboxWorkspace.materialise.__func__

    async def materialise_then_hold_the_lock(**kwargs):
        ws = await real(SandboxWorkspace, **kwargs)
        await backend._lock.acquire()
        wrapped.set()
        return ws

    monkeypatch.setattr(SandboxWorkspace, "materialise", staticmethod(materialise_then_hold_the_lock))
    task = asyncio.create_task(build(backend))
    try:
        await asyncio.wait_for(wrapped.wait(), timeout=5)
        await asyncio.sleep(0.05)               # the build is now queued on the lock it needs for the cache insert
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if backend._lock.locked():
            backend._lock.release()
    assert client.closed == 1 and backend._workspaces == {}


# ---- what must not change --------------------------------------------------------------------------------------------------------

@BUILDS
async def test_a_build_that_finishes_keeps_its_client_open_and_cached(client, build):
    backend = _backend()
    ws = await build(backend)
    assert isinstance(ws, SandboxWorkspace) and backend._workspaces == {"ws-1": ws}
    assert client.connected and client.closed == 0 and client.close_started == 0


async def test_the_caller_that_lost_the_race_closes_its_own_client_and_gets_the_winner(monkeypatch, client):
    winner = object()
    backend = _backend()
    real = SandboxWorkspace.materialise.__func__

    async def materialise_while_another_caller_wins(**kwargs):
        ws = await real(SandboxWorkspace, **kwargs)
        backend._workspaces["ws-1"] = winner         # the other caller got to the cache first
        return ws

    monkeypatch.setattr(SandboxWorkspace, "materialise", staticmethod(materialise_while_another_caller_wins))
    assert await _reattach(backend) is winner
    assert client.closed == 1 and backend._workspaces == {"ws-1": winner}


async def test_two_concurrent_gets_on_a_cold_cache_share_one_workspace_and_only_the_loser_closes(monkeypatch):
    """A REAL race: two callers re-attach at once, each with its own client; materialise waits until both have arrived, so
    both reach the cache insert. Both get the same workspace; the connection that was cached stays open (it is the shared one),
    and only the other caller's client is closed. (A stand-in winner object cannot tell the two clients apart: a build that
    closed the winner's would pass.)"""
    clients: list[_Client] = []

    def new_client(**kwargs) -> _Client:
        clients.append(_Client())
        return clients[-1]

    monkeypatch.setattr(k8s_backend, "RuntimeClient", new_client)
    backend = _backend()
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
    first, second = await asyncio.wait_for(asyncio.gather(_reattach(backend), _reattach(backend)), timeout=10)
    assert first is second and backend._workspaces == {"ws-1": first}
    assert len(clients) == 2
    cached = first._sandbox._client
    (other,) = [c for c in clients if c is not cached]
    assert cached.close_started == 0 and cached.closed == 0, "the cached connection is the shared one: it stays open"
    assert other.closed == 1, "the caller that lost the race closed its own"


async def test_a_cancel_during_the_losers_close_does_not_close_it_a_second_time(monkeypatch):
    close_gate = asyncio.Event()
    c = _Client(close_gate=close_gate)
    monkeypatch.setattr(k8s_backend, "RuntimeClient", lambda **kwargs: c)
    backend = _backend()
    real = SandboxWorkspace.materialise.__func__

    async def materialise_while_another_caller_wins(**kwargs):
        ws = await real(SandboxWorkspace, **kwargs)
        backend._workspaces["ws-1"] = object()
        return ws

    monkeypatch.setattr(SandboxWorkspace, "materialise", staticmethod(materialise_while_another_caller_wins))
    task = asyncio.create_task(_reattach(backend))
    for _ in range(100):
        if c.close_started:
            break
        await asyncio.sleep(0.01)
    assert c.close_started == 1 and not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert c.close_started == 1, "the build did not start a second close of a client that is already being closed"
    close_gate.set()
    for _ in range(100):
        if c.closed:
            break
        await asyncio.sleep(0.01)
    assert c.closed == 1


# ---- the close itself --------------------------------------------------------------------------------------------------------------

@BUILDS
async def test_a_close_that_fails_is_logged_and_does_not_replace_the_error_that_ended_the_build(monkeypatch, build, caplog):
    c = _Client(close_error=OSError("the socket was already gone"))
    monkeypatch.setattr(k8s_backend, "RuntimeClient", lambda **kwargs: c)
    _materialise_raises(monkeypatch, RuntimeError("the sandbox could not be wrapped"))
    with caplog.at_level(logging.WARNING, logger="primer.workspace"):
        with pytest.raises(RuntimeError, match="could not be wrapped"):
            await build(_backend())
    assert c.close_started == 1
    assert any("aclose failed" in r.getMessage() for r in caplog.records)


@BUILDS
async def test_a_second_cancel_does_not_leave_the_close_half_done(monkeypatch, build):
    """A drain that cancels again while the client is closing must not abandon the close: it runs on."""
    close_gate = asyncio.Event()
    c = _Client(close_gate=close_gate)
    monkeypatch.setattr(k8s_backend, "RuntimeClient", lambda **kwargs: c)
    entered = _materialise_blocks(monkeypatch)
    task = asyncio.create_task(build(_backend()))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    for _ in range(100):                          # the build is now inside the close, which waits on the gate
        if c.close_started:
            break
        await asyncio.sleep(0.01)
    assert c.close_started == 1 and not task.done()
    task.cancel()                                  # the second cancel
    with pytest.raises(asyncio.CancelledError):
        await task
    assert c.closed == 0, "the close was still waiting when the task ended"
    close_gate.set()
    for _ in range(100):
        if c.closed:
            break
        await asyncio.sleep(0.01)
    assert c.closed == 1, "the close carried on after the second cancel"
