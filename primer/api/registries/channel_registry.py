"""Caches one ``ChannelAdapter`` per ``Channel`` row.

Pattern matches ``ProviderRegistry`` / ``SemanticSearchRegistry``:
lazy adapter construction on first access, double-checked locking
to avoid duplicate adapter loops on concurrent first-touch, and
``invalidate(channel_id=...)`` to flush a single entry when the
operator edits the row.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from primer.channel.adapter import ChannelAdapter
from primer.channel.factory import build_adapter
from primer.int.storage import Storage
from primer.model.channel import (
    Channel,
    ChannelProvider,
    WorkspaceChannelAssociation,
)
from primer.model.except_ import NotFoundError
from primer.model.storage import FieldRef, OffsetPage, Op, Predicate, Value


if TYPE_CHECKING:
    from primer.channel.inbox import ChannelInbox


logger = logging.getLogger(__name__)


class ChannelRegistry:
    """Per-row adapter registry."""

    def __init__(
        self,
        *,
        channel_storage: Storage[Channel],
        channel_provider_storage: Storage[ChannelProvider],
        association_storage: Storage[WorkspaceChannelAssociation],
        inbox: "ChannelInbox",
        storage_provider: object | None = None,
        event_bus: object | None = None,
        claim_engine: object | None = None,
    ) -> None:
        self._channels = channel_storage
        self._providers = channel_provider_storage
        self._associations = association_storage
        self._inbox = inbox
        self._storage_provider = storage_provider
        self._event_bus = event_bus
        self._claim_engine = claim_engine
        self._adapters: dict[str, ChannelAdapter] = {}
        self._lock = asyncio.Lock()

    def set_claim_engine(self, claim_engine: object | None) -> None:
        """Late-bind the claim engine (built after this registry at boot)."""
        self._claim_engine = claim_engine

    def peek_adapter(self, channel_id: str) -> ChannelAdapter | None:
        """Return the already-built adapter for ``channel_id``, or None.

        Cache-only: unlike :meth:`get_adapter` this never builds or
        ``initialize()``-s an adapter. The chat-relay path uses it so an
        out-of-proc worker (which deliberately does NOT warm inbound channel
        gateways) cannot lazily open a *second* inbound connection just to
        post an outbound message. When this returns None the caller routes
        the relay over the event bus to the process that owns the warm
        adapter instead of building one here.
        """
        return self._adapters.get(channel_id)

    async def get_adapter(self, channel_id: str) -> ChannelAdapter:
        cached = self._adapters.get(channel_id)
        if cached is not None:
            return cached
        async with self._lock:
            cached = self._adapters.get(channel_id)
            if cached is not None:
                return cached
            channel = await self._channels.get(channel_id)
            if channel is None:
                raise NotFoundError(
                    f"Channel {channel_id!r} does not exist"
                )
            provider = await self._providers.get(channel.provider_id)
            if provider is None:
                raise NotFoundError(
                    f"ChannelProvider {channel.provider_id!r} does not exist"
                )
            adapter = await build_adapter(
                provider, channel, self._inbox,
                storage_provider=self._storage_provider,
                event_bus=self._event_bus,
                claim_engine=self._claim_engine,
            )
            await adapter.initialize()
            self._adapters[channel_id] = adapter
            return adapter

    async def for_workspace(
        self, workspace_id: str,
    ) -> list[tuple[ChannelAdapter, WorkspaceChannelAssociation]]:
        page = await self._associations.find(
            Predicate(
                left=Predicate(
                    left=FieldRef(name="workspace_id"),
                    op=Op.EQ,
                    right=Value(value=workspace_id),
                ),
                op=Op.AND,
                right=Predicate(
                    left=FieldRef(name="enabled"),
                    op=Op.EQ,
                    right=Value(value=True),
                ),
            ),
            OffsetPage(offset=0, length=200),
        )
        result: list[tuple[ChannelAdapter, WorkspaceChannelAssociation]] = []
        for assoc in page.items:
            try:
                adapter = await self.get_adapter(assoc.channel_id)
            except Exception as exc:
                logger.warning(
                    "ChannelRegistry: build_adapter failed for %s: %s",
                    assoc.channel_id, exc,
                )
                continue
            result.append((adapter, assoc))
        return result

    async def warm_chat_channels(self) -> int:
        """Eagerly start the adapter for every enabled ChatChannelAssociation
        so chat-driven bots come online (poll / connect) at boot.

        Session channels are warmed by the first outbound park; a chat is
        user-initiated and has no other start trigger, so without this the bot
        would never begin receiving messages. Per-adapter failures are logged,
        not raised. Returns the count of adapters started. No-op when no
        storage_provider is wired.
        """
        if self._storage_provider is None:
            return 0
        from primer.model.channel import ChatChannelAssociation

        page = await self._storage_provider.get_storage(
            ChatChannelAssociation,
        ).find(
            Predicate(
                left=FieldRef(name="enabled"), op=Op.EQ,
                right=Value(value=True),
            ),
            OffsetPage(offset=0, length=200),
        )
        started = 0
        for assoc in page.items:
            try:
                await self.get_adapter(assoc.channel_id)
                started += 1
            except Exception as exc:
                logger.warning(
                    "warm_chat_channels: failed to start %s: %s",
                    assoc.channel_id, exc,
                )
        return started

    async def invalidate(self, *, channel_id: str | None = None) -> None:
        async with self._lock:
            if channel_id is None:
                ids = list(self._adapters.keys())
            else:
                ids = [channel_id] if channel_id in self._adapters else []
            for cid in ids:
                adapter = self._adapters.pop(cid)
                try:
                    await adapter.aclose()
                except Exception:
                    logger.exception(
                        "ChannelRegistry: invalidate aclose for %s", cid,
                    )

    async def aclose(self) -> None:
        await self.invalidate()


__all__ = ["ChannelRegistry"]
