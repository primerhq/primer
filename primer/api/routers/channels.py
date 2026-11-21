"""REST routers for ChannelProvider and Channel CRUD."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.exceptions import RequestValidationError

from primer.api.deps import get_storage_provider
from primer.api.routers._crud import make_crud_router
from primer.api.route_tools import CrudToolDeclaration
from primer.api.routers._references import ReferenceCheck
from primer.api.routers.access import make_access_router
from primer.access.person import person_from_request
from primer.channel.checks import check_channel_on_create, check_channel_on_update, check_channel_provider_secrets
from primer.common.entity_checks import EntityCheckError
from primer.int.coordinator import InvalidationTopic
from primer.model.channel import (
    Channel,
    ChannelProvider,
)
from primer.model.common import preserve_masked_secrets
from primer.model.except_ import ConflictError



# ---------------------------------------------------------------------------
# Storage dependency helpers
# ---------------------------------------------------------------------------


def _get_channel_provider_storage(request: Request):
    return get_storage_provider(request).get_storage(ChannelProvider)


def _get_channel_storage(request: Request):
    return get_storage_provider(request).get_storage(Channel)


# ---------------------------------------------------------------------------
# Post-commit hints wake registries after the authorized storage operation.
# ---------------------------------------------------------------------------


def _channel_registry(request: Request):
    """Return the per-process ChannelRegistry, or None when channels are not
    wired on this app (e.g. minimal test apps). Invalidation is then a no-op."""
    return getattr(request.app.state, "channel_registry", None)


async def _channel_changed(entity_id: str, request: Request) -> None:
    registry = _channel_registry(request)
    if registry is not None:
        await registry.changed(InvalidationTopic.CHANNEL, entity_id)


async def _provider_changed(entity_id: str, request: Request) -> None:
    registry = _channel_registry(request)
    if registry is not None:
        await registry.changed(InvalidationTopic.CHANNEL_PROVIDER, entity_id)


# ---------------------------------------------------------------------------
# ChannelProvider router
# ---------------------------------------------------------------------------


async def _channel_provider_on_pre_create(entity: ChannelProvider, request: Request) -> None:
    """The secrets that will be stored must be real tokens (the create route has already refused a served mask)."""
    del request
    try:
        check_channel_provider_secrets(entity)
    except EntityCheckError as exc:
        raise _as_rest_error(exc, at_field=True) from exc


async def _channel_provider_on_pre_update(entity: ChannelProvider, existing: ChannelProvider, request: Request) -> None:
    """Restore first, then validate (audit G2-01).

    An edit that leaves a token alone sends back the mask a GET served; the model lets it through, this puts the stored token back,
    and then the value that will be stored is checked, so a mask that was not restored is refused and never stored.
    """
    del request
    preserve_masked_secrets(entity, existing)
    try:
        check_channel_provider_secrets(entity)
    except EntityCheckError as exc:
        raise _as_rest_error(exc, at_field=True) from exc


def make_channel_provider_router() -> APIRouter:
    return make_crud_router(
        model_cls=ChannelProvider,
        grantable=True,
        storage_dep=_get_channel_provider_storage,
        plural="channel_providers",
        tool_declaration=CrudToolDeclaration(
            resource_id="channel_providers",
            generated_operations=frozenset({"find"}),
            curated_tool_ids={
                "create": ("system__create_channel_provider",),
                "get": ("system__get_channel_provider",),
                "list": ("system__list_channel_providers",),
                "update": ("system__update_channel_provider",),
                "delete": ("system__delete_channel_provider",),
            },
        ),
        tag="channel_providers",
        on_pre_create=_channel_provider_on_pre_create,
        on_pre_update=_channel_provider_on_pre_update,
        on_create=_provider_changed,
        on_update=_provider_changed,
        on_delete=_provider_changed,
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
    # The check is shared with the system tools (primer/channel/checks.py); this re-raises exactly what it always raised: a 422 whose
    # detail is a plain string for a refused body, a ConflictError for a duplicate.
    try:
        await check_channel_on_create(entity, storage_provider=get_storage_provider(request), person=person_from_request(request))
    except EntityCheckError as exc:
        raise _as_rest_error(exc) from exc


async def _channel_on_pre_update(entity: Channel, existing: Channel, request: Request) -> None:
    """A replace must name an existing ChannelProvider, that provider's platform, and a ``(provider_id, external_id)`` pair no OTHER
    channel holds (tickets 01a1139e and 01a11598-e1d7-7060-8fe9-d483dec4cf09).

    The router had no pre-update hook, so a PUT could move a channel to another platform, onto a provider that does not exist, or onto
    another channel's pair (a 409 now, where create always refused it).
    """
    # The new pair is what counts (the row is rewritten whole); the saver is asked only about a provider the update adds.
    try:
        await check_channel_on_update(
            entity, storage_provider=get_storage_provider(request), person=person_from_request(request), existing=existing,
        )
    except EntityCheckError as exc:
        raise _as_rest_error(exc) from exc


def _as_rest_error(exc: EntityCheckError, *, at_field: bool = False) -> Exception:
    """A ``ConflictError`` for a clash, else a 422.

    The channel routes answer a 422 whose detail is the plain message (the shape they have always used). The channel provider's
    secret check (``at_field``) answers a refusal that names its field as the request-validation envelope at that field's body
    path, ``body.config.<name>`` (ticket 01a16b64 R5), the shape the routers answer for a refused body field, so a client can
    show it under the field instead of a detail that does not say which one.
    """
    if exc.kind == "conflict":
        return ConflictError(exc.message)
    if at_field and exc.field:
        return RequestValidationError([{"type": "value_error", "loc": ("body", *exc.field.split(".")), "msg": exc.message}])
    return HTTPException(status_code=422, detail=exc.message)


def make_channel_router() -> APIRouter:
    return make_crud_router(
        model_cls=Channel,
        storage_dep=_get_channel_storage,
        plural="channels",
        tool_declaration=CrudToolDeclaration(
            resource_id="channels",
            generated_operations=frozenset({"find"}),
            curated_tool_ids={
                "create": ("system__create_channel",),
                "get": ("system__get_channel",),
                "list": ("system__list_channels",),
                "update": ("system__update_channel",),
                "delete": ("system__delete_channel",),
            },
        ),
        tag="channels",
        on_pre_create=_channel_on_pre_create,
        on_pre_update=_channel_on_pre_update,
        on_create=_channel_changed,
        on_update=_channel_changed,
        on_delete=_channel_changed,
        owned=True,
    )


def make_channel_access_router() -> APIRouter:
    # GET/PUT /channels/{id}/access: mounted BEFORE the channel CRUD router.
    return make_access_router(
        model_cls=Channel,
        plural="channels",
        storage_dep=_get_channel_storage,
        tag="channels",
        tool_resource_id="channels",
    )


__all__ = [
    "make_channel_provider_router",
    "make_channel_router",
    "make_channel_access_router",
]
