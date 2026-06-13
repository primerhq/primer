"""REST routers for ChannelProvider and Channel CRUD."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from primer.api.deps import get_storage_provider
from primer.api.routers._crud import make_crud_router
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
# ChannelProvider router
# ---------------------------------------------------------------------------


def make_channel_provider_router() -> APIRouter:
    return make_crud_router(
        model_cls=ChannelProvider,
        storage_dep=_get_channel_provider_storage,
        plural="channel_providers",
        tag="channel_providers",
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
    )


__all__ = [
    "make_channel_provider_router",
    "make_channel_router",
]
