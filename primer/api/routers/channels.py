"""REST routers for ChannelProvider and Channel CRUD."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from primer.api.deps import get_storage_provider
from primer.api.routers._crud import make_crud_router
from primer.api.routers._references import ReferenceCheck
from primer.model.channel import (
    Channel,
    ChannelProvider,
)
from primer.model.except_ import ConflictError
from primer.model.storage import OffsetPage
from primer.storage.q import Q


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Storage dependency helpers
# ---------------------------------------------------------------------------


def _get_channel_provider_storage(request: Request):
    return get_storage_provider(request).get_storage(ChannelProvider)


def _get_channel_storage(request: Request):
    return get_storage_provider(request).get_storage(Channel)


# ---------------------------------------------------------------------------
# Warm-adapter invalidation hooks
#
# Channel / ChannelProvider adapters warm once and cache their live gateway
# (Discord/Slack WS, Telegram poller). A config edit through these routers must
# flush the warm adapter so the next inbound/relay rebuilds it lazily with the
# new config; otherwise the running process keeps serving the stale connection
# until restart.
# ---------------------------------------------------------------------------


def _channel_registry(request: Request):
    """Return the per-process ChannelRegistry, or None when channels are not
    wired on this app (e.g. minimal test apps). Invalidation is then a no-op."""
    return getattr(request.app.state, "channel_registry", None)


def _owns_inbound(request: Request) -> bool:
    """True when this process owns inbound channel gateways and may therefore
    open one live.

    Opening a chat adapter starts its inbound listener (Telegram long-poll /
    Slack socket / Discord gateway). A worker-only process must NOT do that --
    it would be a SECOND inbound connection competing with the API's (Telegram
    409, duplicate Slack/Discord deliveries); worker-only processes relay
    outbound over the bus instead. Mirrors the ``owns_inbound`` gate that
    ``_app_lifespan`` uses for the startup ``warm_chat_channels`` call. When the
    config is absent (minimal test apps) default to True so the re-warm path is
    exercised under test."""
    config = getattr(request.app.state, "config", None)
    mode = getattr(config, "runtime_mode", None) if config is not None else None
    if mode is None:
        return True
    from primer.model.scheduler import RuntimeMode

    return mode in (RuntimeMode.API, RuntimeMode.API_PLUS_WORKER)


async def _invalidate_channel(entity_id: str, request: Request) -> None:
    """Flush the warm adapter for the edited/deleted channel so it rebuilds
    lazily with the new config.

    Used as the DELETE hook: the row is going away, so we only close any warm
    adapter (no re-warm). The UPDATE hook is :func:`_invalidate_and_rewarm_channel`,
    which additionally brings a newly chat-enabled channel online live."""
    registry = _channel_registry(request)
    if registry is not None:
        await registry.invalidate(channel_id=entity_id)


async def _invalidate_and_rewarm_channel(entity_id: str, request: Request) -> None:
    """Channel UPDATE hook: flush the stale warm adapter, then -- if the
    freshly-saved row now has ``config.chats.enabled`` -- re-warm its inbound
    gateway live so enabling chats no longer needs a server restart.

    A chat is user-initiated, so unlike a session channel there is no outbound
    park to lazily warm it; ``warm_chat_channels`` (boot only) was previously
    the sole trigger. Re-warming here closes that gap. Best-effort: the registry
    helper logs and swallows build failures so the CRUD response never fails;
    the re-warm is also gated to inbound-owning processes (:func:`_owns_inbound`)
    so a worker-only process never opens a competing inbound connection."""
    registry = _channel_registry(request)
    if registry is None:
        return
    # Invalidate first so a config edit on an already-warm channel still drops
    # the stale gateway (the 2fa4338f behaviour); the re-warm then rebuilds it
    # fresh against the new config when chats are enabled.
    await registry.invalidate(channel_id=entity_id)
    if not _owns_inbound(request):
        return
    await registry.rewarm_if_chat_enabled(entity_id)


async def _invalidate_provider_channels(entity_id: str, request: Request) -> None:
    """A provider edit (e.g. rotated bot token) affects every channel that
    shares its connection, so flush the whole warm-adapter cache; each channel
    rebuilds lazily against the new provider config on next use."""
    registry = _channel_registry(request)
    if registry is not None:
        await registry.invalidate(channel_id=None)


# ---------------------------------------------------------------------------
# ChannelProvider router
# ---------------------------------------------------------------------------


def make_channel_provider_router() -> APIRouter:
    return make_crud_router(
        model_cls=ChannelProvider,
        storage_dep=_get_channel_provider_storage,
        plural="channel_providers",
        tag="channel_providers",
        on_update=_invalidate_provider_channels,
        on_delete=_invalidate_provider_channels,
        references=[
            # Cascade-block: a ChannelProvider must not be deleted while a
            # Channel still references it via ``provider_id`` (§3 invariant).
            # Restores the guard collaterally dropped in ddb91310 when the
            # WorkspaceChannelAssociation routers were removed.
            ReferenceCheck(
                child_kind="channel",
                child_storage=_get_channel_storage,
                child_field="provider_id",
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Channel router
# ---------------------------------------------------------------------------


async def _channel_on_pre_create(entity: Channel, request: Request) -> None:
    """Enforce (provider_id, external_id) uniqueness and provider existence.

    Also defaults ``entity.provider`` from the referenced ChannelProvider
    row when the caller omitted it.
    """
    sp = get_storage_provider(request)
    # Check provider existence.
    provider_storage = sp.get_storage(ChannelProvider)
    provider = await provider_storage.get(entity.provider_id)
    if provider is None:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=422,
            detail=f"ChannelProvider {entity.provider_id!r} does not exist",
        )
    # Default entity.provider from the ChannelProvider row when unset.
    if entity.provider is None:
        object.__setattr__(entity, "provider", provider.provider)
    # Check (provider_id, external_id) uniqueness.
    channel_storage = sp.get_storage(Channel)
    page = await channel_storage.find(
        Q(Channel)
        .where("provider_id", entity.provider_id)
        .where("external_id", entity.external_id)
        .build(),
        OffsetPage(offset=0, length=1),
    )
    if page.items:
        raise ConflictError(
            f"Channel with provider_id={entity.provider_id!r}, "
            f"external_id={entity.external_id!r} already exists "
            f"(id={page.items[0].id!r})"
        )


def make_channel_router() -> APIRouter:
    return make_crud_router(
        model_cls=Channel,
        storage_dep=_get_channel_storage,
        plural="channels",
        tag="channels",
        on_pre_create=_channel_on_pre_create,
        on_update=_invalidate_and_rewarm_channel,
        on_delete=_invalidate_channel,
    )


__all__ = [
    "make_channel_provider_router",
    "make_channel_router",
]
