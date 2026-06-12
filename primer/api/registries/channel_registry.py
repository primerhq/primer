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
    ) -> None:
        self._channels = channel_storage
        self._providers = channel_provider_storage
        self._associations = association_storage
        self._inbox = inbox
        self._storage_provider = storage_provider
        self._event_bus = event_bus
        self._adapters: dict[str, ChannelAdapter] = {}
        self._lock = asyncio.Lock()

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
