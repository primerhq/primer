"""Manage per-room adapters against the persisted channel and provider configuration."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from primer.channel.adapter import ChannelAdapter
from primer.channel.factory import build_adapter
from primer.channel.retirement import drain_retirements, finish_cleanup
from primer.int.storage import Storage
from primer.model.channel import (
    Channel,
    ChannelProvider,
)
from primer.model.channel_correlation import ChannelCorrelation
from primer.model.workspace import Workspace
from primer.model.workspace_session import WorkspaceSession
from primer.storage.q import Q
from primer.model.except_ import NotFoundError
from primer.model.storage import CursorPage, OffsetPage
from primer.int.coordinator import InvalidationTopic


if TYPE_CHECKING:
    from primer.int.coordinator import InvalidationBus, InvalidationSubscription
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
        owns_inbound: bool = True,
    ) -> None:
        self._channels = channel_storage
        #: False in a worker-only process (W7): its adapters take outbound-only connections (no poller, gateway or
        #: socket) and install no inbound handlers, so they never compete with the API process for inbound events.
        self.owns_inbound = owns_inbound
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
        self._rows: dict[str, tuple[Channel, ChannelProvider]] = {}
        self._wake = {False: asyncio.Event(), True: asyncio.Event()}
        self._draining = False
        self._retry: dict[str, tuple[Channel, ChannelProvider, float, float]] = {}
        self._bus: InvalidationBus | None = None
        self._subscriptions: list[InvalidationSubscription] = []

    def set_event_bus(self, event_bus: object) -> None:
        self._event_bus = event_bus

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

        This inspection never opens a connection or changes listener ownership.
        """
        return None if self._draining else self._adapters.get(channel_id)

    async def get_adapter(self, channel_id: str) -> ChannelAdapter:
        async with self._lock:
            if self._draining:
                raise RuntimeError("channel connections are draining after an ownership change")
            cached = self._adapters.get(channel_id)
            if cached is not None:
                return cached
            channel = await self._channels.get(channel_id)
            if channel is None:
                raise NotFoundError(f"Channel {channel_id!r} does not exist")
            provider = await self._providers.get(channel.provider_id)
            if provider is None:
                raise NotFoundError(f"ChannelProvider {channel.provider_id!r} does not exist")
            return await self._build_locked(channel, provider)

    async def _build_locked(self, channel: Channel, provider: ChannelProvider) -> ChannelAdapter:
        # A lazy caller may see a rotation before the notification arrives. Drain all stale
        # references before acquiring the shared provider client with the new credentials.
        for cid, (_, old) in list(self._rows.items()):
            if old.id == provider.id and old.config != provider.config:
                await self._close_locked(cid)
        adapter = await build_adapter(
            provider, channel, self._inbox, storage_provider=self._storage_provider,
            event_bus=self._event_bus, claim_engine=self._claim_engine,
            artifact_registry=self._artifact_registry, workspace_registry=self._workspace_registry,
            scheduler=self._scheduler, owns_inbound=self.owns_inbound,
        )
        adapter.stt_registry = self._stt_registry
        self._adapters[channel.id] = adapter
        self._rows[channel.id] = (channel, provider)
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
            snapshot = self._rows.get(binding.channel_id)
            if not self.owns_inbound and snapshot is not None and not snapshot[0].config.chats.enabled:
                # The owner can reconstruct this destination from the persisted binding.
                await self.changed(InvalidationTopic.CHANNEL, binding.channel_id)
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

    async def _reply_rooms(self, candidates: list[str]) -> set[str]:
        """Find cold disabled rooms with durable reply routes, in bounded groups."""
        needed = set()
        for i in range(0, len(candidates), 200):
            remaining = set(candidates[i:i + 200])
            correlations = self._storage_provider.get_storage(ChannelCorrelation)
            needed.update(await correlations.count_by("channel_id", sorted(remaining)))
            remaining -= needed
            if not remaining:
                continue
            query = Q.or_(Q(WorkspaceSession).where_null("metadata.reply_binding.silent"),
                          Q(WorkspaceSession).where("metadata.reply_binding.silent", False))
            predicate = query.where_in("metadata.reply_binding.channel_id", sorted(remaining)).build()
            cursor = None
            while remaining:
                page = await self._storage_provider.get_storage(WorkspaceSession).find(
                    predicate, CursorPage(cursor=cursor, length=200),
                )
                for session in page.items:
                    cid = session.metadata["reply_binding"]["channel_id"]
                    needed.add(cid)
                    remaining.discard(cid)
                cursor = page.next_cursor
                if cursor is None:
                    break

        # The model accepts both stored names (reply_binding and legacy channel_association).
        # Filtering on the current JSON field would discard old rows before model validation.
        remaining = set(candidates) - needed
        cursor = None
        while remaining:
            page = await self._storage_provider.get_storage(Workspace).list(CursorPage(cursor=cursor, length=200))
            for workspace in page.items:
                binding = workspace.reply_binding
                if binding is not None and binding.channel_id in remaining:
                    needed.add(binding.channel_id)
                    remaining.remove(binding.channel_id)
            cursor = page.next_cursor
            if cursor is None:
                break
        return needed

    async def reconcile(self) -> int:
        """Close stale adapters before warming rooms. Nonowners only inspect their outbound cache."""
        if self._storage_provider is None:
            return 0
        async with self._lock:
            if self._draining:
                await self._drain_locked()
            channels: dict[str, Channel] = {}
            if self.owns_inbound:
                offset = 0
                while True:
                    page = await self._channels.list(OffsetPage(offset=offset, length=200, want_total=False))
                    channels.update((row.id, row) for row in page.items)
                    if len(page.items) < 200:
                        break
                    offset += len(page.items)
            else:
                for cid in self._rows:
                    row = await self._channels.get(cid)
                    if row is not None:
                        channels[cid] = row

            providers = {}
            for pid in {row.provider_id for row in channels.values()}:
                providers[pid] = await self._providers.get(pid)
            for cid, (old_channel, old_provider) in list(self._rows.items()):
                current = channels.get(cid)
                provider = providers.get(old_provider.id)
                if current != old_channel or provider is None or provider.config != old_provider.config:
                    await self._close_locked(cid)

            for cid in self._retry.keys() - channels.keys():
                del self._retry[cid]
            missing = [cid for cid in channels if self.owns_inbound and cid not in self._adapters]

        reply_rooms = await self._reply_rooms([cid for cid in missing if not channels[cid].config.chats.enabled])
        started = 0
        for cid in missing:
            if not channels[cid].config.chats.enabled and cid not in reply_rooms:
                continue
            # Release the registry between starts. The list above can now be stale; a
            # deleted, disabled or reassigned room must never be warmed from that snapshot.
            async with self._lock:
                if not self.owns_inbound or cid in self._adapters:
                    continue
                row = await self._channels.get(cid)
                if row is None or (not row.config.chats.enabled and cid not in reply_rooms):
                    continue
                provider = await self._providers.get(row.provider_id)
                if provider is None:
                    continue
                retry = self._retry.get(cid)
                delay = 30.0
                if retry and (row, provider) == retry[:2]:
                    if asyncio.get_running_loop().time() < retry[2]:
                        continue
                    delay = min(300.0, retry[3] * 2)
                try:
                    async with asyncio.timeout(45):
                        await self._build_locked(row, provider)
                except Exception as exc:
                    self._retry[cid] = (row, provider, asyncio.get_running_loop().time() + delay, delay)
                    # SDK error strings can contain credentials in a URL.
                    logger.warning("channel %s listener start failed (%s); will retry", cid, type(exc).__name__)
                else:
                    self._retry.pop(cid, None)
                    started += 1
                    logger.info("channel %s listener started", cid)
        return started

    async def watch(self, *, inbound: bool) -> None:
        """Notifications are hints. A missed publish or failed start heals on the next scan."""
        while True:
            self._wake[inbound].clear()
            if self.owns_inbound == inbound:
                try:
                    await self.reconcile()
                except Exception as exc:
                    logger.warning("channel reconciliation failed (%s); will retry", type(exc).__name__)
            try:
                await asyncio.wait_for(self._wake[inbound].wait(), timeout=30)
            except TimeoutError:
                pass

    def _wake_watchers(self) -> None:
        for wake in self._wake.values():
            wake.set()

    async def bind_invalidation_bus(self, bus: InvalidationBus) -> None:
        async def changed(_key: str) -> None:
            self._wake_watchers()

        try:
            for topic in (InvalidationTopic.CHANNEL, InvalidationTopic.CHANNEL_PROVIDER):
                self._subscriptions.append(await bus.subscribe(topic, changed, on_reconnect=self._wake_watchers))
        except BaseException:
            await self.aclose()
            raise
        self._bus = bus

    async def changed(self, topic: InvalidationTopic, key: str) -> None:
        """Post-commit hint. Rows remain authoritative if the writer dies or the bus is unavailable."""
        self._wake_watchers()
        if self._bus is not None:
            try:
                async with asyncio.timeout(1):
                    await self._bus.publish(topic, key)
            except Exception:
                logger.warning("channel change notification failed for %s; periodic reconciliation will retry", key)

    async def set_inbound(self, enabled: bool) -> None:
        async with self._lock:
            # The SDK shares one connection per provider. Drain outbound-only references
            # before promotion, and every listening connection before lease release.
            if self.owns_inbound != enabled or self._draining:
                self.owns_inbound = False
                self._draining = True
                await finish_cleanup(self._drain_locked())
                self.owns_inbound = enabled
                logger.info("channel listener ownership %s", "acquired" if enabled else "released")
        self._wake_watchers()

    async def _drain_locked(self) -> None:
        # On a close failure the registry stays outbound and refuses cached clients
        # until the next reconciliation finishes this transition.
        results = await asyncio.gather(
            *(self._close_locked(cid) for cid in list(self._adapters)), return_exceptions=True,
        )
        await drain_retirements()
        for result in results:
            if isinstance(result, BaseException):
                raise result
        self._retry.clear()
        self._draining = False

    async def _close_locked(self, cid: str) -> None:
        # One SDK release per reference, even if the supervisor loses its lease while
        # the last reference is closing. Finish the release before unlocking the cache.
        close = asyncio.create_task(self._adapters[cid].aclose())
        cancellation = None
        while not close.done():
            try:
                await asyncio.shield(close)
            except asyncio.CancelledError as exc:
                cancellation = exc
        close.result()
        self._adapters.pop(cid)
        self._rows.pop(cid, None)
        if cancellation is not None:
            raise cancellation

    async def invalidate(self, *, channel_id: str | None = None) -> None:
        async with self._lock:
            ids = list(self._adapters) if channel_id is None else [channel_id]
            for cid in ids:
                if cid in self._adapters:
                    await self._close_locked(cid)

    async def aclose(self) -> None:
        for subscription in self._subscriptions:
            await subscription.aclose()
        self._subscriptions.clear()
        self._bus = None
        await self.invalidate()


__all__ = ["ChannelRegistry"]
