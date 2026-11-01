"""Provider-level shared Slack Socket Mode connection.

Slack allows ~2 concurrent Socket Mode connections per app. To
keep primer's connection count bounded, every ChannelAdapter for
the same ChannelProvider shares ONE connection.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from primer.model.channel import ChannelProvider, SlackChannelProviderConfig


if TYPE_CHECKING:
    pass


logger = logging.getLogger(__name__)


class _Conn:
    """The AsyncApp (``app``: its handlers and its web client) and its Socket Mode handler, None when posting only (W7)."""

    def __init__(self, app: Any, handler: Any) -> None:
        self.app = app
        self.handler = handler

    async def start_async(self) -> None:
        if self.handler is not None:
            await self.handler.connect_async()

    async def close_async(self) -> None:
        if self.handler is not None:
            await self.handler.close_async()


def _build_app(cfg: SlackChannelProviderConfig) -> Any:
    """A slack_bolt AsyncApp for the bot token. Deferred import so module-import doesn't pay the cost."""
    from slack_bolt.async_app import AsyncApp

    return AsyncApp(token=cfg.bot_token.get_secret_value())


def _build_app_and_handler(cfg: SlackChannelProviderConfig) -> Any:
    """An AsyncApp plus its AsyncSocketModeHandler (connected by ``start_async``), so callers can register handlers."""
    from slack_bolt.adapter.socket_mode.async_handler import (
        AsyncSocketModeHandler,
    )

    app = _build_app(cfg)
    return _Conn(app, AsyncSocketModeHandler(app, cfg.app_token.get_secret_value()))


def _build_outbound_app(cfg: SlackChannelProviderConfig) -> Any:
    """The AsyncApp alone, for posting: no Socket Mode handler, so this process receives no events (W7)."""
    return _Conn(_build_app(cfg), None)


@dataclass
class _Entry:
    conn: Any
    refcount: int = 0
    adapters_by_channel_id: dict[str, Any] = field(default_factory=dict)


class _SlackConnectionRegistry:
    """Per-provider shared Slack connection registry."""

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()
        #: Called with the provider id once its connection is closed: the factory then forgets it installed handlers on it, so a
        #: reopened connection gets them again (W8).
        self.on_close: Callable[[str], None] | None = None

    async def acquire(self, provider: ChannelProvider, *, inbound: bool = True) -> Any:
        """Return the (shared) connection. Increments the refcount.

        ``inbound=False`` opens no Socket Mode connection (W7). shortcut: the first acquire of a provider decides the
        mode and later ones share it; a process has one ChannelRegistry, so one mode.
        """
        async with self._lock:
            entry = self._entries.get(provider.id)
            if entry is None:
                cfg = provider.config
                assert isinstance(cfg, SlackChannelProviderConfig)
                conn = _build_app_and_handler(cfg) if inbound else _build_outbound_app(cfg)
                await conn.start_async()
                entry = _Entry(conn=conn, refcount=0)
                self._entries[provider.id] = entry
                logger.info(
                    "slack: connection opened for provider %s", provider.id,
                )
            entry.refcount += 1
            return entry.conn

    async def release(self, provider: ChannelProvider) -> None:
        """Decrement the refcount; close on zero."""
        async with self._lock:
            entry = self._entries.get(provider.id)
            if entry is None:
                return
            entry.refcount -= 1
            if entry.refcount <= 0:
                try:
                    await entry.conn.close_async()
                except Exception:
                    logger.exception(
                        "slack: connection close failed for %s", provider.id,
                    )
                del self._entries[provider.id]
                if self.on_close is not None:
                    self.on_close(provider.id)
                logger.info(
                    "slack: connection closed for provider %s", provider.id,
                )

    def entry(self, provider_id: str) -> _Entry | None:
        return self._entries.get(provider_id)


# One module-global instance — singleton across the process.
SLACK_CONNECTIONS = _SlackConnectionRegistry()


__all__ = ["SLACK_CONNECTIONS", "_SlackConnectionRegistry"]
