"""REST routers for ChannelProvider, Channel, and WorkspaceChannelAssociation CRUD."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends, Path, Request, status

from matrix.api.deps import get_storage_provider
from matrix.api.errors import common_responses
from matrix.api.routers._crud import make_crud_router
from matrix.model.channel import (
    Channel,
    ChannelProvider,
    WorkspaceChannelAssociation,
)
from matrix.model.except_ import ConflictError, NotFoundError
from matrix.model.storage import FieldRef, OffsetPage, Op, Predicate, Value


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


async def _channel_provider_on_delete(entity_id: str, request: Request) -> None:
    """Block delete when any Channel still references this provider."""
    sp = get_storage_provider(request)
    channel_storage = sp.get_storage(Channel)
    page = await channel_storage.find(
        Predicate(
            left=FieldRef(name="provider_id"),
            op=Op.EQ,
            right=Value(value=entity_id),
        ),
        OffsetPage(offset=0, length=1),
    )
    if page.items:
        raise ConflictError(
            f"ChannelProvider {entity_id!r} cannot be deleted while "
            f"Channel {page.items[0].id!r} still references it"
        )


def make_channel_provider_router() -> APIRouter:
    return make_crud_router(
        model_cls=ChannelProvider,
        storage_dep=_get_channel_provider_storage,
        plural="channel_providers",
        tag="channel_providers",
        on_delete=_channel_provider_on_delete,
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
        Predicate(
            left=Predicate(
                left=FieldRef(name="provider_id"),
                op=Op.EQ,
                right=Value(value=entity.provider_id),
            ),
            op=Op.AND,
            right=Predicate(
                left=FieldRef(name="external_id"),
                op=Op.EQ,
                right=Value(value=entity.external_id),
            ),
        ),
        OffsetPage(offset=0, length=1),
    )
    if page.items:
        raise ConflictError(
            f"Channel with provider_id={entity.provider_id!r}, "
            f"external_id={entity.external_id!r} already exists "
            f"(id={page.items[0].id!r})"
        )


async def _channel_on_delete(entity_id: str, request: Request) -> None:
    """Block delete when any WorkspaceChannelAssociation references this channel."""
    sp = get_storage_provider(request)
    assoc_storage = sp.get_storage(WorkspaceChannelAssociation)
    page = await assoc_storage.find(
        Predicate(
            left=FieldRef(name="channel_id"),
            op=Op.EQ,
            right=Value(value=entity_id),
        ),
        OffsetPage(offset=0, length=1),
    )
    if page.items:
        raise ConflictError(
            f"Channel {entity_id!r} cannot be deleted while "
            f"WorkspaceChannelAssociation {page.items[0].id!r} still "
            "references it"
        )


def make_channel_router() -> APIRouter:
    return make_crud_router(
        model_cls=Channel,
        storage_dep=_get_channel_storage,
        plural="channels",
        tag="channels",
        on_pre_create=_channel_on_pre_create,
        on_delete=_channel_on_delete,
    )


# ---------------------------------------------------------------------------
# WorkspaceChannelAssociation router
# ---------------------------------------------------------------------------
# The association is scoped under /v1/workspaces/{wid}/channel_associations.
# make_crud_router doesn't support a scope_field knob, so we write the
# endpoints manually and use the flat /v1/workspace_channel_associations
# path internally while adding a thin proxy at the scoped path.
# ---------------------------------------------------------------------------


async def _association_on_pre_create(
    entity: WorkspaceChannelAssociation, request: Request,
) -> None:
    """Enforce (workspace_id, channel_id) uniqueness."""
    sp = get_storage_provider(request)
    assoc_storage = sp.get_storage(WorkspaceChannelAssociation)
    page = await assoc_storage.find(
        Predicate(
            left=Predicate(
                left=FieldRef(name="workspace_id"),
                op=Op.EQ,
                right=Value(value=entity.workspace_id),
            ),
            op=Op.AND,
            right=Predicate(
                left=FieldRef(name="channel_id"),
                op=Op.EQ,
                right=Value(value=entity.channel_id),
            ),
        ),
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
    2. A scoped proxy router at /v1/workspaces/{wid}/channel_associations
       that handles POST (create) scoped to the workspace.
    """
    router = APIRouter(tags=["workspace_channel_associations"])

    # Flat CRUD router.
    flat = make_crud_router(
        model_cls=WorkspaceChannelAssociation,
        storage_dep=_get_association_storage,
        plural="workspace_channel_associations",
        tag="workspace_channel_associations",
        on_pre_create=_association_on_pre_create,
    )
    router.include_router(flat)

    # Scoped proxy: POST /v1/workspaces/{wid}/channel_associations
    # Accepts the full WorkspaceChannelAssociation body; the wid path param
    # must match workspace_id in the body (or we fill it from the path).
    @router.post(
        "/workspaces/{wid}/channel_associations",
        response_model=WorkspaceChannelAssociation,
        status_code=status.HTTP_201_CREATED,
        summary="Create WorkspaceChannelAssociation scoped to workspace",
        responses=common_responses(409, 422, 500),
    )
    async def _scoped_create(
        request: Request,
        wid: str = Path(..., description="Workspace id"),
        entity: WorkspaceChannelAssociation = Body(
            ..., description="WorkspaceChannelAssociation body"
        ),
        storage=Depends(_get_association_storage),
    ) -> WorkspaceChannelAssociation:
        # Populate workspace_id from the path if not set / mismatched.
        if entity.workspace_id != wid:
            entity = entity.model_copy(update={"workspace_id": wid})
        existing = await storage.get(entity.id)
        if existing is not None:
            raise ConflictError(
                f"WorkspaceChannelAssociation with id {entity.id!r} already exists"
            )
        await _association_on_pre_create(entity, request)
        return await storage.create(entity)

    return router


__all__ = [
    "make_channel_provider_router",
    "make_channel_router",
    "make_workspace_channel_association_router",
]
