"""Pre-create checks for :class:`~primer.model.channel.Channel`, shared by the REST router and the system tools (task 01a111d1, D5
phase 2b).

Moved out of ``primer/api/routers/channels.py``, where it was bound to a ``Request``: ``check_channel_on_create`` takes
``(entity, storage_provider)`` and raises :class:`~primer.common.entity_checks.EntityCheckError`. The router's hook re-raises the
exact exceptions it always raised (an ``HTTPException(422)`` whose detail is a plain string for a missing provider, a
``ConflictError`` for a duplicate); the system ``create_channel`` tool answers ``validation-error`` / ``conflict``.

There is no pre-update check, in REST or here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from primer.common.entity_checks import EntityCheckError
from primer.model.channel import Channel, ChannelProvider
from primer.model.storage import OffsetPage
from primer.storage.q import Q

if TYPE_CHECKING:
    from primer.int.storage_provider import StorageProvider


async def check_channel_on_create(entity: Channel, *, storage_provider: "StorageProvider") -> None:
    """Enforce provider existence and ``(provider_id, external_id)`` uniqueness.

    Also defaults ``entity.provider`` from the referenced ChannelProvider row when the caller omitted it (mutates ``entity``; the
    model requires the field, so this branch only matters for an entity built without validation).
    """
    provider = await storage_provider.get_storage(ChannelProvider).get(entity.provider_id)
    if provider is None:
        raise EntityCheckError("validation", f"ChannelProvider {entity.provider_id!r} does not exist")
    if entity.provider is None:
        object.__setattr__(entity, "provider", provider.provider)
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


__all__ = ["check_channel_on_create"]
