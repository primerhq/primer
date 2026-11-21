"""Provider-level shared Discord Client registry."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from primer.common.shielded import run_in_background
from primer.channel.retirement import check_available, retire, retire_failed_start
from primer.model.channel import (
    ChannelProvider, DiscordChannelProviderConfig,
)


logger = logging.getLogger(__name__)

#: How long a caller waits for the cleanup of a gateway start that did not finish (stopping the connect task, closing the logged-in
#: client: an HTTP session and a websocket) before it carries on and leaves the cleanup running.
_START_CLEANUP_WAIT_S = 10.0


def _build_client(cfg: DiscordChannelProviderConfig) -> Any:
    """Construct a discord.Client with the required intents."""
    import discord

    intents = discord.Intents.none()
    intents.guilds = True
    intents.guild_messages = True
    intents.message_content = True
    if cfg.enable_dms:
        intents.dm_messages = True
    return discord.Client(intents=intents)


async def _start_client_as_task(
    client: Any, token: str, *, ready_wait: float = 30.0, retire_start=None,
) -> asyncio.Task:
    """Start the gateway connection on a background task; await ready.

    ``login`` first so the client runs discord.py's async setup hook (which
    creates the internal ready event) before we wait on it; only then run the
    gateway loop via ``connect`` on a background task. Calling
    ``wait_until_ready`` on an unlogged-in client raises "Client has not been
    properly initialised", so ``client.start`` (login + connect) cannot be
    create_task'd and immediately waited on.

    A start that does not finish (a failed login, the ready timeout, a cancel or a caller's ``asyncio.timeout``: the relay builds
    an adapter inside its 15-second post bound, shorter than the 30-second ready wait) is undone: the connect task is stopped and
    the logged-in client closed, on their own task and with a bounded wait (``run_in_background``), so nothing keeps a gateway
    session open that no registry entry points at, and the next acquire does not open a second one.
    """
    task: asyncio.Task | None = None
    try:
        await client.login(token)
        task = asyncio.create_task(client.connect())
        # Wait for the gateway to reach READY (or timeout).
        try:
            await asyncio.wait_for(client.wait_until_ready(), timeout=ready_wait)
        except TimeoutError as exc:
            raise RuntimeError("discord gateway ready timeout") from exc
        return task
    except BaseException:
        if retire_start is not None:
            await retire_start(lambda: _discard_client(client, task))
        else:
            await run_in_background(
                _discard_client(client, task), what="discord gateway start", verb="cleanup", wait_s=_START_CLEANUP_WAIT_S,
            )
        raise


async def _login_client(client: Any, token: str, *, retire_start=None) -> None:
    """Log in for REST calls only (W7): no gateway, so this process receives no events. A failed login closes the client."""
    try:
        await client.login(token)
    except BaseException:
        if retire_start is not None:
            await retire_start(lambda: _discard_client(client, None))
        else:
            await run_in_background(
                _discard_client(client, None), what="discord login", verb="cleanup", wait_s=_START_CLEANUP_WAIT_S,
            )
        raise


async def _discard_client(client: Any, task: asyncio.Task | None) -> None:
    """Stop both the client and the connect loop. A failure leaves cleanup pending."""
    if task is not None and not task.done():
        task.cancel()
    await client.close()
    if task is not None:
        await asyncio.wait({task})
        if not task.cancelled():
            task.exception()


@dataclass
class _Entry:
    client: Any
    task: asyncio.Task | None = None
    refcount: int = 0
    adapters_by_channel_id: dict[str, Any] = field(default_factory=dict)


class _DiscordConnectionRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()
        #: Called with the provider id once its connection is closed: the factory then forgets it installed handlers on it, so a
        #: reopened connection gets them again (W8).
        self.on_close: Callable[[str], None] | None = None

    async def acquire(self, provider: ChannelProvider, *, inbound: bool = True) -> Any:
        """The provider's shared client; refcounted. ``inbound=False`` logs in without a gateway (W7).

        shortcut: the first acquire of a provider decides the mode and later ones share it; a process has one
        ChannelRegistry, so one mode.
        """
        async with self._lock:
            check_available(self, provider.id)
            entry = self._entries.get(provider.id)
            if entry is None:
                cfg = provider.config
                assert isinstance(cfg, DiscordChannelProviderConfig)
                client = _build_client(cfg)
                async def retire_start(close):
                    await retire_failed_start(self, provider.id, close)

                task = None
                try:
                    token = cfg.bot_token.get_secret_value()
                    if inbound:
                        task = await _start_client_as_task(client, token, retire_start=retire_start)
                    else:
                        await _login_client(client, token, retire_start=retire_start)
                except Exception:
                    logger.exception(
                        "discord: failed to start client for %s", provider.id,
                    )
                    raise
                entry = _Entry(client=client, task=task)
                self._entries[provider.id] = entry
            entry.refcount += 1
            return entry.client

    async def release(self, provider: ChannelProvider, *, expected: Any = None) -> None:
        async with self._lock:
            entry = self._entries.get(provider.id)
            if entry is None or (expected is not None and entry.client is not expected):
                return
            entry.refcount -= 1
            if entry.refcount:
                return
            del self._entries[provider.id]
            closing = retire(self, provider.id, lambda: _discard_client(entry.client, entry.task))
            if self.on_close is not None:
                self.on_close(provider.id)
        await asyncio.shield(closing)

    def entry(self, provider_id: str) -> _Entry | None:
        return self._entries.get(provider_id)


DISCORD_CONNECTIONS = _DiscordConnectionRegistry()


__all__ = ["DISCORD_CONNECTIONS", "_DiscordConnectionRegistry"]
