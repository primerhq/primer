"""Provider-level shared Discord Client registry."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from matrix.model.channel import (
    ChannelProvider, DiscordChannelProviderConfig,
)


logger = logging.getLogger(__name__)


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
    """Start the gateway connection on a background task; await ready."""
    task = asyncio.create_task(client.start(token))
    # Wait for the client to login + ready (or timeout).
    try:
        await asyncio.wait_for(client.wait_until_ready(), timeout=ready_wait)
    except asyncio.TimeoutError as exc:
        task.cancel()
        raise RuntimeError("discord gateway ready timeout") from exc
    return task


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
                except Exception as exc:
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
                try:
                    await entry.client.close()
                except Exception:
                    logger.exception(
                        "discord: close failed for %s", provider.id,
                    )
                if entry.task is not None:
                    entry.task.cancel()
                del self._entries[provider.id]

    def entry(self, provider_id: str) -> _Entry | None:
        return self._entries.get(provider_id)


DISCORD_CONNECTIONS = _DiscordConnectionRegistry()


__all__ = ["DISCORD_CONNECTIONS", "_DiscordConnectionRegistry"]
