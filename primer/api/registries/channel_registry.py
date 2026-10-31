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
)
from primer.model.except_ import NotFoundError
from primer.model.storage import OffsetPage


if TYPE_CHECKING:
    from primer.channel.inbox import ChannelInbox
    from primer.channel.reply_binding import ReplyBinding


logger = logging.getLogger(__name__)


class ChannelRegistry:
    """Per-row adapter registry."""

    def __init__(
        self,
        *,
        channel_storage: Storage[Channel],
        channel_provider_storage: Storage[ChannelProvider],
        inbox: "ChannelInbox",
        storage_provider: object | None = None,
        event_bus: object | None = None,
        claim_engine: object | None = None,
        artifact_registry: object | None = None,
        workspace_registry: object | None = None,
        scheduler: object | None = None,
    ) -> None:
        self._channels = channel_storage
        self._providers = channel_provider_storage
        self._inbox = inbox
        self._storage_provider = storage_provider
        self._event_bus = event_bus
        self._claim_engine = claim_engine
        self._artifact_registry = artifact_registry
        self._workspace_registry = workspace_registry
        self._scheduler = scheduler
        self._stt_registry: object | None = None
        self._adapters: dict[str, ChannelAdapter] = {}
        self._lock = asyncio.Lock()

    def set_claim_engine(self, claim_engine: object | None) -> None:
        """Late-bind the claim engine (built after this registry at boot)."""
        self._claim_engine = claim_engine

    def set_artifact_registry(self, artifact_registry: object | None) -> None:
        """Late-bind the artifact registry (built after this registry at boot)."""
        self._artifact_registry = artifact_registry

    def set_session_wiring(
        self, *, workspace_registry: object | None, scheduler: object | None,
        stt_registry: object | None = None,
    ) -> None:
        """Late-bind the session collaborators (built after this registry).

        Mirrors :meth:`set_claim_engine`. Adapters cached before this call
        keep the wiring they were built with, so call it before warming.
        """
        self._workspace_registry = workspace_registry
        self._scheduler = scheduler
        self._stt_registry = stt_registry

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
                artifact_registry=self._artifact_registry,
                workspace_registry=self._workspace_registry,
                scheduler=self._scheduler,
            )
            adapter.stt_registry = self._stt_registry
            await adapter.initialize()
            self._adapters[channel_id] = adapter
            return adapter

    async def route_for_session(self, session) -> tuple["ReplyBinding | None", list[ChannelAdapter]]:
        """Return ``session``'s reply binding and the channel adapter(s) it names.

        Resolves through :func:`resolve_reply_binding`, so the
        session-ephemeral binding (``session.metadata``) takes precedence
        over the workspace-standing :attr:`Workspace.reply_binding`. The
        binding comes back with the adapters so the dispatcher can put its
        anchor on every post. The adapter list is empty when no binding
        resolves, when the binding is silent (a binding's ``reply_target``
        ``none``) or when the adapter cannot be built (failure is isolated:
        the binding is still returned).
        """
        if self._storage_provider is None:
            return None, []
        from primer.channel.reply_binding import resolve_reply_binding

        binding = await resolve_reply_binding(
            session, storage_provider=self._storage_provider,
        )
        if binding is None:
            return None, []
        if binding.silent:
            return binding, []          # reply_target "none": nothing is posted, and no adapter is built for it
        try:
            adapter = await self.get_adapter(binding.channel_id)
        except Exception as exc:
            logger.warning(
                "ChannelRegistry: build_adapter failed for %s: %s",
                binding.channel_id, exc,
            )
            return binding, []
        return binding, [adapter]

    async def route_for_workspace(self, workspace_id: str) -> tuple["ReplyBinding | None", list[ChannelAdapter]]:
        """:meth:`route_for_session` for a post with no session: the workspace-standing binding.

        Builds a lightweight stand-in carrying ``workspace_id`` and empty
        ``metadata`` so the workspace-standing reply-binding path is
        exercised through the one resolution path.
        """
        if self._storage_provider is None:
            return None, []

        class _WorkspaceStandIn:
            def __init__(self, workspace_id: str) -> None:
                self.workspace_id = workspace_id
                self.metadata: dict = {}

        return await self.route_for_session(_WorkspaceStandIn(workspace_id))

    async def for_session(self, session) -> list[ChannelAdapter]:
        """The channel adapter(s) for ``session``'s reply binding (:meth:`route_for_session` without the binding)."""
        _binding, adapters = await self.route_for_session(session)
        return adapters

    async def for_workspace(
        self, workspace_id: str,
    ) -> list[ChannelAdapter]:
        """The channel adapter for this workspace's standing reply binding, if any (:meth:`route_for_workspace`)."""
        _binding, adapters = await self.route_for_workspace(workspace_id)
        return adapters

    async def warm_chat_channels(self) -> int:
        """Eagerly start the adapter for every Channel whose
        ``config.chats.enabled`` is True so chat-driven bots come online
        (poll / connect) at boot.

        Session channels are warmed by the first outbound park; a chat is
        user-initiated and has no other start trigger, so without this the bot
        would never begin receiving messages. Per-adapter failures are logged,
        not raised. Returns the count of adapters started. No-op when no
        storage_provider is wired.
        """
        if self._storage_provider is None:
            return 0

        channel_storage = self._storage_provider.get_storage(Channel)
        started = 0
        offset = 0
        while True:
            page = await channel_storage.list(OffsetPage(offset=offset, length=200))
            items = list(page.items)
            for channel in items:
                cfg = getattr(channel, "config", None)
                chats = getattr(cfg, "chats", None) if cfg is not None else None
                if chats is None or not getattr(chats, "enabled", False):
                    continue
                try:
                    await self.get_adapter(channel.id)
                    started += 1
                except Exception as exc:
                    logger.warning(
                        "warm_chat_channels: failed to start %s: %s",
                        channel.id, exc,
                    )
            if len(items) < 200:
                break
            offset += 200
        return started

    async def rewarm_if_chat_enabled(self, channel_id: str) -> bool:
        """Re-warm the inbound adapter for ``channel_id`` iff its persisted
        ``config.chats.enabled`` is True. Returns True when an adapter was
        (re)built, False otherwise.

        Used by the channel UPDATE hook: a chat is user-initiated and has no
        outbound park to lazily warm it, so flipping ``chats.enabled`` false ->
        true on an existing channel would otherwise stay dark until the next
        ``warm_chat_channels`` (boot only). After :meth:`invalidate` has dropped
        any stale adapter, this brings the fresh gateway online live by reading
        the post-write row and calling :meth:`get_adapter` (build +
        ``initialize()``) when chats are enabled. Best-effort: a build failure
        is logged, not raised, so it never fails the CRUD response. No-op (and
        returns False) when no storage_provider is wired or chats are disabled.

        Mirrors the per-channel enabled-check + ``get_adapter`` of
        :meth:`warm_chat_channels` for a single row.
        """
        if self._storage_provider is None:
            return False
        try:
            channel = await self._channels.get(channel_id)
        except Exception as exc:
            logger.warning(
                "rewarm_if_chat_enabled: failed to read %s: %s",
                channel_id, exc,
            )
            return False
        if channel is None:
            return False
        cfg = getattr(channel, "config", None)
        chats = getattr(cfg, "chats", None) if cfg is not None else None
        if chats is None or not getattr(chats, "enabled", False):
            return False
        try:
            # get_adapter is idempotent: if invalidate already cleared the
            # stale entry this rebuilds fresh; if something re-warmed it
            # first, the cached adapter is returned (no double-warm).
            await self.get_adapter(channel_id)
        except Exception as exc:
            logger.warning(
                "rewarm_if_chat_enabled: failed to start %s: %s",
                channel_id, exc,
            )
            return False
        return True

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
