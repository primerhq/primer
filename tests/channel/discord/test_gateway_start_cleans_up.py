"""A Discord gateway start that does not finish leaves no connect task and no logged-in client behind.

``_start_client_as_task`` logs in, starts ``client.connect()`` on a background task and waits for READY. Only the ready TIMEOUT
cancelled the task; any other exit (a cancel or a caller's ``asyncio.timeout``, a failed login) left the gateway loop running with
no handler and no registry entry, and no exit closed the logged-in client (its HTTP session, a websocket): the next acquire logged
in again and opened another gateway session. It is reachable from the channel relay, which builds an adapter inside its
15-second post bound, shorter than the 30-second ready wait. The registry's ``release`` also left a stale entry holding a closed
client when its close was cancelled.

These use fake clients: they need no ``discord`` package and no gateway.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import SecretStr

from primer.channel.discord import connection
from primer.channel.discord.connection import _DiscordConnectionRegistry, _start_client_as_task
from primer.model.channel import ChannelProvider, ChannelProviderType, DiscordChannelProviderConfig


class _Client:
    """What ``_start_client_as_task`` calls on a discord.Client, with the failure points a test needs."""

    def __init__(self, *, login_gate: asyncio.Event | None = None, login_error: Exception | None = None,
                 ready: bool = False, close_gate: asyncio.Event | None = None, close_error: Exception | None = None) -> None:
        self.login_gate, self.login_error, self.ready_ever = login_gate, login_error, ready
        self.close_gate, self.close_error = close_gate, close_error
        self.logged_in = False
        self.connect_started = asyncio.Event()
        self.connect_cancelled = False
        self.waiting_for_ready = asyncio.Event()
        self.close_started = 0
        self.closed = 0

    async def login(self, token: str) -> None:
        if self.login_gate is not None:
            await self.login_gate.wait()
        if self.login_error is not None:
            raise self.login_error
        self.logged_in = True

    async def connect(self) -> None:
        self.connect_started.set()
        try:
            await asyncio.Event().wait()  # the gateway loop
        except asyncio.CancelledError:
            self.connect_cancelled = True
            raise

    async def wait_until_ready(self) -> None:
        self.waiting_for_ready.set()
        if not self.ready_ever:
            await asyncio.Event().wait()

    async def close(self) -> None:
        self.close_started += 1
        if self.close_gate is not None:
            await self.close_gate.wait()
        if self.close_error is not None:
            raise self.close_error
        self.closed += 1


def _gateway_tasks() -> list[asyncio.Task]:
    return [t for t in asyncio.all_tasks() if "connect" in repr(t.get_coro()) and not t.done()]


async def _until(condition, what: str) -> None:
    for _ in range(500):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"never happened: {what}")


def _provider(id_: str = "cp-1") -> ChannelProvider:
    return ChannelProvider(
        id=id_, provider=ChannelProviderType.DISCORD, config=DiscordChannelProviderConfig(bot_token=SecretStr("a" * 60)),
    )


# ---- _start_client_as_task ---------------------------------------------------------------------------------------------------------

async def test_a_caller_timeout_while_waiting_for_ready_stops_the_gateway_task_and_closes_the_client():
    """The lead's probe: a client whose ready never comes under an outer ``asyncio.timeout``: it left one connect task running."""
    client = _Client()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            await _start_client_as_task(client, "tok", ready_wait=30.0)
    assert client.connect_cancelled and client.closed == 1 and _gateway_tasks() == []


async def test_a_cancel_while_waiting_for_ready_is_cleaned_up_too():
    client = _Client()
    task = asyncio.create_task(_start_client_as_task(client, "tok", ready_wait=30.0))
    await asyncio.wait_for(client.waiting_for_ready.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert client.connect_cancelled and client.closed == 1 and _gateway_tasks() == []


async def test_the_ready_timeout_still_raises_runtime_error_and_now_closes_the_client():
    client = _Client()
    with pytest.raises(RuntimeError, match="discord gateway ready timeout"):
        await _start_client_as_task(client, "tok", ready_wait=0.05)
    assert client.connect_cancelled and client.closed == 1 and _gateway_tasks() == []


async def test_a_failed_login_closes_the_client_and_starts_no_gateway_task():
    """``login`` opens the HTTP session before it can fail."""
    client = _Client(login_error=RuntimeError("improper token has been passed"))
    with pytest.raises(RuntimeError, match="improper token"):
        await _start_client_as_task(client, "tok")
    assert not client.connect_started.is_set() and client.closed == 1


async def test_a_cancel_during_login_closes_the_client():
    client = _Client(login_gate=asyncio.Event())
    task = asyncio.create_task(_start_client_as_task(client, "tok"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert not client.connect_started.is_set() and client.closed == 1


async def test_a_start_that_finishes_keeps_its_gateway_task_and_client():
    client = _Client(ready=True)
    task = await _start_client_as_task(client, "tok")
    try:
        assert not task.done() and client.closed == 0 and not client.connect_cancelled
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_a_second_cancel_during_the_cleanup_does_not_abandon_it():
    close_gate = asyncio.Event()
    client = _Client(close_gate=close_gate)
    task = asyncio.create_task(_start_client_as_task(client, "tok", ready_wait=30.0))
    await asyncio.wait_for(client.waiting_for_ready.wait(), timeout=5)
    try:
        task.cancel()
        await _until(lambda: client.close_started, "the cleanup reached the close")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
    finally:
        close_gate.set()
    await _until(lambda: client.closed == 1, "the close finished after the second cancel")


async def test_a_cleanup_that_hangs_does_not_hold_the_caller_past_its_bound(monkeypatch):
    monkeypatch.setattr(connection, "_START_CLEANUP_WAIT_S", 0.05)
    close_gate = asyncio.Event()
    client = _Client(login_error=RuntimeError("improper token has been passed"), close_gate=close_gate)
    try:
        with pytest.raises(RuntimeError, match="improper token"):
            await asyncio.wait_for(_start_client_as_task(client, "tok"), timeout=2)
        assert client.close_started == 1 and client.closed == 0
    finally:
        close_gate.set()
    await _until(lambda: client.closed == 1, "the close finished in the background")


async def test_a_close_that_fails_does_not_replace_the_error_that_ended_the_start():
    client = _Client(login_error=RuntimeError("improper token has been passed"), close_error=OSError("session already closed"))
    with pytest.raises(RuntimeError, match="improper token"):
        await _start_client_as_task(client, "tok")
    assert client.close_started == 1


# ---- the registry ------------------------------------------------------------------------------------------------------------------

def _registry(monkeypatch, clients: list[_Client]) -> _DiscordConnectionRegistry:
    """A registry whose clients are ``clients`` in turn, started with the REAL ``_start_client_as_task``."""
    queue = iter(clients)
    monkeypatch.setattr(connection, "_build_client", lambda cfg: next(queue))
    return _DiscordConnectionRegistry()


async def test_a_cancelled_acquire_caches_nothing_and_releases_the_lock(monkeypatch):
    stuck, fine = _Client(), _Client(ready=True)
    registry = _registry(monkeypatch, [stuck, fine])
    first = asyncio.create_task(registry.acquire(_provider()))
    await asyncio.wait_for(stuck.waiting_for_ready.wait(), timeout=5)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(first, timeout=5)
    assert registry.entry("cp-1") is None and stuck.closed == 1 and stuck.connect_cancelled
    # nothing is left behind it: the next acquire logs in once, with a client of its own
    got = await asyncio.wait_for(registry.acquire(_provider()), timeout=5)
    assert got is fine and registry.entry("cp-1").refcount == 1
    await registry.release(_provider())


async def test_a_failed_acquire_caches_nothing(monkeypatch):
    broken, fine = _Client(login_error=RuntimeError("improper token has been passed")), _Client(ready=True)
    registry = _registry(monkeypatch, [broken, fine])
    with pytest.raises(RuntimeError, match="improper token"):
        await registry.acquire(_provider())
    assert registry.entry("cp-1") is None and broken.closed == 1
    assert await registry.acquire(_provider()) is fine
    await registry.release(_provider())


async def test_release_removes_the_entry_and_stops_the_gateway_even_when_the_close_is_cancelled(monkeypatch):
    close_gate = asyncio.Event()
    client = _Client(ready=True, close_gate=close_gate)
    registry = _registry(monkeypatch, [client])
    await registry.acquire(_provider())
    gateway = registry.entry("cp-1").task
    try:
        releasing = asyncio.create_task(registry.release(_provider()))
        await _until(lambda: client.close_started, "release reached the close")
        releasing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(releasing, timeout=5)
        assert registry.entry("cp-1") is None, "a stale entry would hand the next acquire a closed client"
        await _until(gateway.cancelled, "the gateway task was cancelled")
    finally:
        close_gate.set()
    await _until(lambda: client.closed == 1, "the close ran to its end in the background")


async def test_release_removes_the_entry_when_the_close_fails(monkeypatch):
    client = _Client(ready=True, close_error=OSError("session already closed"))
    registry = _registry(monkeypatch, [client])
    await registry.acquire(_provider())
    gateway = registry.entry("cp-1").task
    await registry.release(_provider())
    assert registry.entry("cp-1") is None
    await _until(gateway.cancelled, "the gateway task was cancelled")
