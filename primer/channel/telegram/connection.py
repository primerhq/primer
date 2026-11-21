"""Provider-level shared Telegram Application registry.

One PTB Application per ChannelProvider — Telegram permits only
one concurrent ``getUpdates`` poll per bot token.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from primer.channel.retirement import check_available, retire, retire_failed_start
from primer.model.channel import (
    ChannelProvider, TelegramChannelProviderConfig,
)


logger = logging.getLogger(__name__)

#: The longest a Telegram request answered 429 (``RetryAfter``) waits before its one retry (W38).
_RETRY_AFTER_CAP_S = 30.0


def _build_application(cfg: TelegramChannelProviderConfig) -> Any:
    """Construct a PTB Application. Deferred import keeps the
    module-import cheap when telegram isn't installed.
    """
    from telegram.error import RetryAfter
    from telegram.ext import Application, BaseRateLimiter, BaseUpdateProcessor

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

    class _RetryAfterOnce(BaseRateLimiter):
        """A request answered 429 waits the delay Telegram names (capped) and is sent once more (W38).

        PTB raises ``RetryAfter`` and the channel dispatcher logs and drops it, so the post was lost. ``getUpdates``
        never comes through here (PTB does not rate-limit it).
        """

        async def initialize(self) -> None:
            pass

        async def shutdown(self) -> None:
            pass

        async def process_request(self, callback, args, kwargs, endpoint, data, rate_limit_args):
            try:
                return await callback(*args, **kwargs)
            except RetryAfter as exc:
                delay = exc.retry_after
                seconds = delay.total_seconds() if isinstance(delay, timedelta) else float(delay)
                await asyncio.sleep(min(seconds, _RETRY_AFTER_CAP_S))
                return await callback(*args, **kwargs)

    builder = (
        Application.builder().token(cfg.bot_token.get_secret_value())
        .concurrent_updates(_PerChatOrder()).rate_limiter(_RetryAfterOnce())
    )
    # PTB's updater handles getUpdates internally; we set the poll
    # timeout on start_polling below.
    return builder.build()


@dataclass
class _Entry:
    app: Any
    refcount: int = 0
    adapters_by_chat_id: dict[str, Any] = field(default_factory=dict)
    poll_timeout: int = 25
    updater_needs_stop: bool = False
    app_needs_stop: bool = False
    shutdown_done: bool = False

    async def stop(self) -> None:
        # Attempt independent stages even if an earlier one failed; retry only
        # unfinished stages. Both PTB stop methods clear running BEFORE draining.
        # Only a successful stop can release an attempted start's obligation.
        try:
            if self.updater_needs_stop:
                await self.app.updater.stop()
                self.updater_needs_stop = False
        finally:
            try:
                if self.app_needs_stop:
                    await self.app.stop()
                    self.app_needs_stop = False
            finally:
                if not self.shutdown_done:
                    await self.app.shutdown()
                    self.shutdown_done = True


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
            check_available(self, provider.id)
            entry = self._entries.get(provider.id)
            if entry is None:
                cfg = provider.config
                assert isinstance(cfg, TelegramChannelProviderConfig)
                app = _build_application(cfg)
                entry = _Entry(app=app, poll_timeout=cfg.poll_timeout_seconds)
                try:
                    await app.initialize()
                    if inbound:
                        entry.app_needs_stop = True
                        await app.start()
                        entry.updater_needs_stop = True
                        await app.updater.start_polling(timeout=cfg.poll_timeout_seconds)
                except BaseException:
                    await retire_failed_start(self, provider.id, entry.stop)
                    raise
                self._entries[provider.id] = entry
                logger.info(
                    "telegram: application started for provider %s", provider.id,
                )
            entry.refcount += 1
            return entry.app

    async def release(self, provider: ChannelProvider, *, expected: Any = None) -> None:
        async with self._lock:
            entry = self._entries.get(provider.id)
            if entry is None or (expected is not None and entry.app is not expected):
                return
            entry.refcount -= 1
            if entry.refcount:
                return
            del self._entries[provider.id]
            closing = retire(self, provider.id, entry.stop)
            if self.on_close is not None:
                self.on_close(provider.id)
        await asyncio.shield(closing)

    def entry(self, provider_id: str) -> _Entry | None:
        return self._entries.get(provider_id)


TELEGRAM_CONNECTIONS = _TelegramConnectionRegistry()


__all__ = ["TELEGRAM_CONNECTIONS", "_TelegramConnectionRegistry"]
