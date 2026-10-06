"""Provider-level shared Discord Client registry."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from primer.common.shielded import run_in_background
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
    client: Any, token: str, *, ready_wait: float = 30.0,
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
        await run_in_background(
            _discard_client(client, task), what="discord gateway start", verb="cleanup", wait_s=_START_CLEANUP_WAIT_S,
        )
        raise


async def _discard_client(client: Any, task: asyncio.Task | None) -> None:
    """Undo a gateway start that did not finish: stop the connect task, wait for it, then close the client. Never raises."""
    if task is not None:
        task.cancel()
        await asyncio.wait({task})
        if not task.cancelled():
            task.exception()  # retrieved: nothing else will read how the gateway loop ended
    try:
        await client.close()
    except Exception:  # noqa: BLE001
        logger.warning("discord: closing the client of a start that did not finish failed", exc_info=True)


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

    async def acquire(self, provider: ChannelProvider) -> Any:
        async with self._lock:
            entry = self._entries.get(provider.id)
            if entry is None:
                cfg = provider.config
                assert isinstance(cfg, DiscordChannelProviderConfig)
                client = _build_client(cfg)
                task = None
                try:
                    task = await _start_client_as_task(
                        client, cfg.bot_token.get_secret_value(),
                    )
                except Exception:
                    logger.exception(
                        "discord: failed to start client for %s", provider.id,
                    )
                    raise
                entry = _Entry(client=client, task=task)
                self._entries[provider.id] = entry
            entry.refcount += 1
            return entry.client

    async def release(self, provider: ChannelProvider) -> None:
        async with self._lock:
            entry = self._entries.get(provider.id)
            if entry is None:
                return
            entry.refcount -= 1
            if entry.refcount <= 0:
                # The entry goes FIRST: whatever the close does (it fails, or the releasing task is cancelled while it runs),
                # a stale entry would hand the next acquire a closed client, and the gateway task is stopped either way.
                del self._entries[provider.id]
                try:
                    # On its own task with a bounded wait: a cancel of the releasing task must not leave the HTTP session
                    # and the websocket half closed.
                    await run_in_background(
                        entry.client.close(), what=f"discord client of {provider.id}", verb="close",
                        wait_s=_START_CLEANUP_WAIT_S,
                    )
                finally:
                    if entry.task is not None:
                        entry.task.cancel()

    def entry(self, provider_id: str) -> _Entry | None:
        return self._entries.get(provider_id)


DISCORD_CONNECTIONS = _DiscordConnectionRegistry()


__all__ = ["DISCORD_CONNECTIONS", "_DiscordConnectionRegistry"]
