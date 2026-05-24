"""Provider-level shared Telegram Application registry.

One PTB Application per ChannelProvider — Telegram permits only
one concurrent ``getUpdates`` poll per bot token.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from matrix.model.channel import (
    ChannelProvider, TelegramChannelProviderConfig,
)


logger = logging.getLogger(__name__)


def _build_application(cfg: TelegramChannelProviderConfig) -> Any:
    """Construct a PTB Application. Deferred import keeps the
    module-import cheap when telegram isn't installed.
    """
    from telegram.ext import Application

    builder = Application.builder().token(cfg.bot_token.get_secret_value())
    # PTB's updater handles getUpdates internally; we set the poll
    # timeout on start_polling below.
    return builder.build()


@dataclass
class _Entry:
    app: Any
    refcount: int = 0
    adapters_by_chat_id: dict[str, Any] = field(default_factory=dict)
    poll_timeout: int = 25


class _TelegramConnectionRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, provider: ChannelProvider) -> Any:
        async with self._lock:
            entry = self._entries.get(provider.id)
            if entry is None:
                cfg = provider.config
                assert isinstance(cfg, TelegramChannelProviderConfig)
                app = _build_application(cfg)
                await app.initialize()
                await app.start()
                # Start long-polling.
                await app.updater.start_polling(timeout=cfg.poll_timeout_seconds)
                entry = _Entry(app=app, poll_timeout=cfg.poll_timeout_seconds)
                self._entries[provider.id] = entry
                logger.info(
                    "telegram: application started for provider %s", provider.id,
                )
            entry.refcount += 1
            return entry.app

    async def release(self, provider: ChannelProvider) -> None:
        async with self._lock:
            entry = self._entries.get(provider.id)
            if entry is None:
                return
            entry.refcount -= 1
            if entry.refcount <= 0:
                try:
                    await entry.app.updater.stop()
                    await entry.app.stop()
                    await entry.app.shutdown()
                except Exception:
                    logger.exception(
                        "telegram: shutdown failed for %s", provider.id,
                    )
                del self._entries[provider.id]
                logger.info(
                    "telegram: application stopped for provider %s", provider.id,
                )

    def entry(self, provider_id: str) -> _Entry | None:
        return self._entries.get(provider_id)


TELEGRAM_CONNECTIONS = _TelegramConnectionRegistry()


__all__ = ["TELEGRAM_CONNECTIONS", "_TelegramConnectionRegistry"]
