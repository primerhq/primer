"""Provider-level shared Telegram Application registry.

One PTB Application per ChannelProvider — Telegram permits only
one concurrent ``getUpdates`` poll per bot token.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from primer.model.channel import (
    ChannelProvider, TelegramChannelProviderConfig,
)


logger = logging.getLogger(__name__)


def _build_application(cfg: TelegramChannelProviderConfig) -> Any:
    """Construct a PTB Application. Deferred import keeps the
    module-import cheap when telegram isn't installed.
    """
    from telegram.ext import Application, BaseUpdateProcessor

    from primer.channel.event_dispatch import _conversation_lock

    class _PerChatOrder(BaseUpdateProcessor):
        """Updates of different chats run concurrently; those of one chat one at a time, in arrival order (W37).

        PTB's default handles one update at a time across every chat, so one voice note (download plus STT) stalled
        every chat and every button of the bot. The chat lock is taken before the concurrency slot, so a busy chat
        queues on its own lock and holds one slot at most.
        """

        def __init__(self) -> None:
            super().__init__(max_concurrent_updates=256)
            self._chats: dict = {}

        async def process_update(self, update: object, coroutine) -> None:
            chat = getattr(update, "effective_chat", None)
            async with _conversation_lock(getattr(chat, "id", None), self._chats):
                await super().process_update(update, coroutine)

        async def do_process_update(self, update: object, coroutine) -> None:
            await coroutine

        async def initialize(self) -> None:
            pass

        async def shutdown(self) -> None:
            pass

    builder = Application.builder().token(cfg.bot_token.get_secret_value()).concurrent_updates(_PerChatOrder())
    # PTB's updater handles getUpdates internally; we set the poll
    # timeout on start_polling below.
    return builder.build()


@dataclass
class _Entry:
    app: Any
    refcount: int = 0
    adapters_by_chat_id: dict[str, Any] = field(default_factory=dict)
    poll_timeout: int = 25
    inbound: bool = True


class _TelegramConnectionRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()
        #: Called with the provider id once its connection is closed: the factory then forgets it installed handlers on it, so a
        #: reopened connection gets them again (W8).
        self.on_close: Callable[[str], None] | None = None

    async def acquire(self, provider: ChannelProvider, *, inbound: bool = True) -> Any:
        """The provider's shared Application; refcounted.

        ``inbound=False`` initializes it for posting only: no update processing and no ``getUpdates`` poll (W7).
        shortcut: the first acquire of a provider decides the mode and later ones share it; a process has one
        ChannelRegistry, so one mode.
        """
        async with self._lock:
            entry = self._entries.get(provider.id)
            if entry is None:
                cfg = provider.config
                assert isinstance(cfg, TelegramChannelProviderConfig)
                app = _build_application(cfg)
                await app.initialize()
                if inbound:
                    await app.start()
                    # Start long-polling.
                    await app.updater.start_polling(timeout=cfg.poll_timeout_seconds)
                entry = _Entry(app=app, poll_timeout=cfg.poll_timeout_seconds, inbound=inbound)
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
                    if entry.inbound:
                        await entry.app.updater.stop()
                        await entry.app.stop()
                    await entry.app.shutdown()
                except Exception:
                    logger.exception(
                        "telegram: shutdown failed for %s", provider.id,
                    )
                del self._entries[provider.id]
                if self.on_close is not None:
                    self.on_close(provider.id)
                logger.info(
                    "telegram: application stopped for provider %s", provider.id,
                )

    def entry(self, provider_id: str) -> _Entry | None:
        return self._entries.get(provider_id)


TELEGRAM_CONNECTIONS = _TelegramConnectionRegistry()


__all__ = ["TELEGRAM_CONNECTIONS", "_TelegramConnectionRegistry"]
