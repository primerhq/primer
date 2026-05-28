"""REST routers for ChannelProvider, Channel, and WorkspaceChannelAssociation CRUD."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from primer.api.deps import get_storage_provider
from primer.api.routers._crud import make_crud_router
from primer.api.routers._references import ReferenceCheck
from primer.model.channel import (
    Channel,
    ChannelProvider,
    WorkspaceChannelAssociation,
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


def _get_association_storage(request: Request):
    return get_storage_provider(request).get_storage(WorkspaceChannelAssociation)


# ---------------------------------------------------------------------------
# ChannelProvider router
# ---------------------------------------------------------------------------


def make_channel_provider_router() -> APIRouter:
    return make_crud_router(
        model_cls=ChannelProvider,
        storage_dep=_get_channel_provider_storage,
        plural="channel_providers",
        tag="channel_providers",
        references=[
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
    """Enforce (provider_id, external_id) uniqueness and provider existence."""
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
        references=[
            ReferenceCheck(
                child_kind="workspace_channel_association",
                child_storage=_get_association_storage,
                child_field="channel_id",
            ),
        ],
    )


# ---------------------------------------------------------------------------
# WorkspaceChannelAssociation router
# ---------------------------------------------------------------------------
# Flat CRUD at /v1/workspace_channel_associations (UI GET/PUT/DELETE paths).
# Scoped CRUD at /v1/workspaces/{wid}/channel_associations via scope_field.
# ---------------------------------------------------------------------------


async def _association_on_pre_create(
    entity: WorkspaceChannelAssociation, request: Request,
) -> None:
    """Enforce (workspace_id, channel_id) uniqueness."""
    sp = get_storage_provider(request)
    assoc_storage = sp.get_storage(WorkspaceChannelAssociation)
    page = await assoc_storage.find(
        Q(WorkspaceChannelAssociation)
        .where("workspace_id", entity.workspace_id)
        .where("channel_id", entity.channel_id)
        .build(),
        OffsetPage(offset=0, length=1),
    )
    if page.items:
        raise ConflictError(
            f"WorkspaceChannelAssociation for workspace_id={entity.workspace_id!r}, "
            f"channel_id={entity.channel_id!r} already exists "
            f"(id={page.items[0].id!r})"
        )


def make_workspace_channel_association_router() -> APIRouter:
    """Returns two routers combined:

    1. A flat CRUD router at /v1/workspace_channel_associations (full CRUD).
    2. A scoped CRUD router at /v1/workspaces/{wid}/channel_associations
       that enforces workspace_id from the path for all CRUD operations.
    """
    router = APIRouter(tags=["workspace_channel_associations"])

    # Flat CRUD router (preserves UI-facing GET/PUT/DELETE paths).
    flat = make_crud_router(
        model_cls=WorkspaceChannelAssociation,
        storage_dep=_get_association_storage,
        plural="workspace_channel_associations",
        tag="workspace_channel_associations",
        on_pre_create=_association_on_pre_create,
    )
    router.include_router(flat)

    # Scoped CRUD router: /v1/workspaces/{parent_id}/channel_associations
    # scope_field enforces that workspace_id in the body matches the path param.
    scoped = make_crud_router(
        model_cls=WorkspaceChannelAssociation,
        storage_dep=_get_association_storage,
        plural="channel_associations",
        tag="workspace_channel_associations",
        scope_field="workspace_id",
        parent_path_segment="workspaces",
        on_pre_create=_association_on_pre_create,
    )
    router.include_router(scoped)

    return router


__all__ = [
    "make_channel_provider_router",
    "make_channel_router",
    "make_workspace_channel_association_router",
]
