"""``_make_ws_sandbox`` closes the RuntimeClient it connected when it does not return a sandbox.

It connects a ``RuntimeClient`` (an aiohttp session and a WebSocket) and then asks the container for its id before wrapping
the client in a ``WSSandbox``. A failure, a cancel or a caller's timeout between the two left the connection open with
nothing holding it: ``get_sandbox`` turns an ``Exception`` into ``None`` (the client still open) and a cancel just
propagates. Both ``create_sandbox`` and ``get_sandbox`` go through this function, so the one fix covers the create and the
re-attach path.
"""

from __future__ import annotations

import asyncio

import pytest

from primer.model.workspace import ContainerReachabilityBridge
from primer.workspace.runtime import docker as docker_mod
from primer.workspace.runtime.ws_sandbox import WSSandbox


class _Client:
    def __init__(self, *, connect_gate: asyncio.Event | None = None) -> None:
        self.connect_gate = connect_gate
        self.connect_started = asyncio.Event()
        self.connected = False
        self.closed = 0

    async def connect(self) -> None:
        self.connect_started.set()
        if self.connect_gate is not None:
            await self.connect_gate.wait()
        self.connected = True

    async def aclose(self) -> None:
        self.closed += 1


class _Container:
    def __init__(self, *, show_error: Exception | None = None, show_blocks: bool = False) -> None:
        self.show_error, self.show_blocks = show_error, show_blocks
        self.show_entered = asyncio.Event()

    async def show(self) -> dict:
        self.show_entered.set()
        if self.show_blocks:
            await asyncio.Event().wait()
        if self.show_error is not None:
            raise self.show_error
        return {"Id": "container-id"}


@pytest.fixture(autouse=True)
def _no_wait_for_the_runtime(monkeypatch):
    async def ready(container, **kwargs):
        return None

    monkeypatch.setattr(docker_mod, "_wait_for_ready", ready)


def _make(container: _Container, client: _Client, monkeypatch):
    monkeypatch.setattr(docker_mod, "RuntimeClient", lambda **kwargs: client)
    return docker_mod._make_ws_sandbox(
        object(), container, "workspace-ws-1", "token", reachability=ContainerReachabilityBridge(network_name="primer-net"),
    )


async def test_a_container_that_cannot_be_inspected_closes_the_client(monkeypatch):
    client = _Client()
    with pytest.raises(RuntimeError, match="gone"):
        await _make(_Container(show_error=RuntimeError("the container is gone")), client, monkeypatch)
    assert client.connected and client.closed == 1


async def test_a_cancel_while_the_container_is_inspected_closes_the_client(monkeypatch):
    client, container = _Client(), _Container(show_blocks=True)
    task = asyncio.create_task(_make(container, client, monkeypatch))
    await asyncio.wait_for(container.show_entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.connected and client.closed == 1


async def test_a_timeout_while_the_container_is_inspected_closes_the_client(monkeypatch):
    client = _Client()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            await _make(_Container(show_blocks=True), client, monkeypatch)
    assert client.closed == 1


async def test_a_cancel_during_connect_closes_the_client(monkeypatch):
    """``connect`` opens the aiohttp session before the handshake; cancelled in the middle, only ``aclose`` releases it."""
    client = _Client(connect_gate=asyncio.Event())
    task = asyncio.create_task(_make(_Container(), client, monkeypatch))
    await asyncio.wait_for(client.connect_started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not client.connected and client.closed == 1


async def test_a_sandbox_that_is_returned_keeps_its_client_open(monkeypatch):
    client = _Client()
    sandbox = await _make(_Container(), client, monkeypatch)
    assert isinstance(sandbox, WSSandbox) and client.connected and client.closed == 0
