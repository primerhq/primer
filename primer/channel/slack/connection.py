"""Provider-level shared Slack Socket Mode connection.

Slack allows ~2 concurrent Socket Mode connections per app. To
keep matrix's connection count bounded, every ChannelAdapter for
the same ChannelProvider shares ONE connection.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from primer.model.channel import ChannelProvider, SlackChannelProviderConfig


if TYPE_CHECKING:
    pass


logger = logging.getLogger(__name__)


def _build_app_and_handler(cfg: SlackChannelProviderConfig) -> Any:
    """Construct + start a slack_bolt AsyncApp + AsyncSocketModeHandler.

    Deferred-imports slack_bolt so module-import doesn't pay the cost.
    The returned object is a small facade exposing ``start_async`` +
    ``close_async`` + ``app`` (the AsyncApp) so callers can register
    handlers.
    """
    from slack_bolt.async_app import AsyncApp
    from slack_bolt.adapter.socket_mode.async_handler import (
        AsyncSocketModeHandler,
    )

    app = AsyncApp(token=cfg.bot_token.get_secret_value())
    handler = AsyncSocketModeHandler(app, cfg.app_token.get_secret_value())

    class _Conn:
        def __init__(self) -> None:
            self.app = app
            self.handler = handler

        async def start_async(self) -> None:
            await handler.connect_async()

        async def close_async(self) -> None:
            await handler.close_async()

    return _Conn()


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

    async def acquire(self, provider: ChannelProvider) -> Any:
        """Return the (shared) connection. Increments the refcount."""
        async with self._lock:
            entry = self._entries.get(provider.id)
            if entry is None:
                cfg = provider.config
                assert isinstance(cfg, SlackChannelProviderConfig)
                conn = _build_app_and_handler(cfg)
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
                logger.info(
                    "slack: connection closed for provider %s", provider.id,
                )

    def entry(self, provider_id: str) -> _Entry | None:
        return self._entries.get(provider_id)


# One module-global instance — singleton across the process.
SLACK_CONNECTIONS = _SlackConnectionRegistry()


__all__ = ["SLACK_CONNECTIONS", "_SlackConnectionRegistry"]
