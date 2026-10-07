"""Pre-write checks for :class:`~primer.model.channel.Channel`, shared by the REST router and the system tools (task 01a111d1, D5
phase 2b; the platform rule is ticket 01a1139e).

Moved out of ``primer/api/routers/channels.py``, where the create check was bound to a ``Request``: each check takes
``(entity, storage_provider)`` and raises :class:`~primer.common.entity_checks.EntityCheckError`. The router's hooks re-raise the
exact exception kinds they always raised (an ``HTTPException(422)`` whose detail is a plain string for a refused body, a
``ConflictError`` for a duplicate); the system ``create_channel`` / ``update_channel`` tools answer ``validation-error`` / ``conflict``.

Create and update both require that the named ChannelProvider exists and that the channel declares that provider's PLATFORM
(slack / discord / telegram). Until the platform rule a slack channel could be stored under a discord provider, and a PUT could move a
channel to another platform or onto a provider that does not exist, because the router had no pre-update hook. Create additionally
enforces that ``(provider_id, external_id)`` is unique; that uniqueness is NOT re-checked on update (a separate gap, not part of the
platform rule).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from primer.common.entity_checks import EntityCheckError
from primer.model.channel import Channel, ChannelProvider
from primer.model.storage import OffsetPage
from primer.storage.q import Q

if TYPE_CHECKING:
    from primer.int.storage_provider import StorageProvider


def _platform(value: object) -> object:
    """A platform as the text a message shows (the enum's value, not its repr)."""
    return getattr(value, "value", value)


async def _stored_provider(entity: Channel, storage_provider: "StorageProvider") -> ChannelProvider:
    provider = await storage_provider.get_storage(ChannelProvider).get(entity.provider_id)
    if provider is None:
        raise EntityCheckError("validation", f"ChannelProvider {entity.provider_id!r} does not exist")
    return provider


def _check_platform_matches(entity: Channel, provider: ChannelProvider) -> None:
    """The channel's declared platform must be the platform of the provider row it names."""
    if entity.provider != provider.provider:
        raise EntityCheckError(
            "validation",
            f"platform {_platform(entity.provider)!r} does not match ChannelProvider {entity.provider_id!r}, "
            f"which is a {_platform(provider.provider)!r} provider",
            field="provider",
        )


async def check_channel_on_create(entity: Channel, *, storage_provider: "StorageProvider") -> None:
    """Enforce provider existence, the platform match, and ``(provider_id, external_id)`` uniqueness, in that order.

    Also defaults ``entity.provider`` from the referenced ChannelProvider row when the caller omitted it (mutates ``entity``; the
    model requires the field, so this branch only matters for an entity built without validation).
    """
    provider = await _stored_provider(entity, storage_provider)
    if entity.provider is None:
        object.__setattr__(entity, "provider", provider.provider)
    _check_platform_matches(entity, provider)
    page = await storage_provider.get_storage(Channel).find(
        Q(Channel)
        .where("provider_id", entity.provider_id)
        .where("external_id", entity.external_id)
        .build(),
        OffsetPage(offset=0, length=1),
    )
    if page.items:
        raise EntityCheckError(
            "conflict",
            f"Channel with provider_id={entity.provider_id!r}, "
            f"external_id={entity.external_id!r} already exists "
            f"(id={page.items[0].id!r})",
        )


async def check_channel_on_update(entity: Channel, *, storage_provider: "StorageProvider") -> None:
    """Enforce provider existence and the platform match on a replace (the row is rewritten whole, so the new pair is what counts)."""
    provider = await _stored_provider(entity, storage_provider)
    _check_platform_matches(entity, provider)


__all__ = ["check_channel_on_create", "check_channel_on_update"]
