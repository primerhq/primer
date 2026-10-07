"""Pre-write checks for :class:`~primer.model.channel.Channel`, shared by the REST router and the system tools (task 01a111d1, D5
phase 2b; the platform rule is ticket 01a1139e).

Moved out of ``primer/api/routers/channels.py``, where the create check was bound to a ``Request``: each check takes
``(entity, storage_provider)`` and raises :class:`~primer.common.entity_checks.EntityCheckError`. The router's hooks re-raise the
exact exception kinds they always raised (an ``HTTPException(422)`` whose detail is a plain string for a refused body, a
``ConflictError`` for a duplicate); the system ``create_channel`` / ``update_channel`` tools answer ``validation-error`` / ``conflict``.

Create and update both require that the named ChannelProvider exists, that the channel declares that provider's PLATFORM
(slack / discord / telegram), and that ``(provider_id, external_id)`` is not held by ANOTHER channel (update ignores the row's own id,
so a channel never conflicts with itself). Until these rules a slack channel could be stored under a discord provider, and a PUT could
move a channel to another platform, onto a provider that does not exist, or onto another channel's pair, because the router had no
pre-update hook. Two rows on one pair are not harmless: inbound events are dispatched through a per-connection dict keyed by external
id that each adapter writes in ``initialize`` and pops unconditionally in ``aclose``, so the last adapter to initialise wins and
closing either one drops the other's route.
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
    await _refuse_if_pair_taken(entity, storage_provider)


async def _refuse_if_pair_taken(
    entity: Channel, storage_provider: "StorageProvider", *, ignore_id: str | None = None,
) -> None:
    """``(provider_id, external_id)`` belongs to one channel. ``ignore_id`` is the row being replaced: it may hold its own pair.

    Two rows are fetched, not one, so a row that is itself on the pair cannot hide ANOTHER holder behind it. On create nothing is
    ignored, and the first row found is named exactly as before, so the message of a create refusal is unchanged.
    """
    page = await storage_provider.get_storage(Channel).find(
        Q(Channel)
        .where("provider_id", entity.provider_id)
        .where("external_id", entity.external_id)
        .build(),
        OffsetPage(offset=0, length=2),
    )
    holder = next((row for row in page.items if row.id != ignore_id), None)
    if holder is not None:
        raise EntityCheckError(
            "conflict",
            f"Channel with provider_id={entity.provider_id!r}, "
            f"external_id={entity.external_id!r} already exists "
            f"(id={holder.id!r})",
        )


async def check_channel_on_update(entity: Channel, *, storage_provider: "StorageProvider") -> None:
    """Enforce provider existence, the platform match and pair uniqueness on a replace, in that order.

    The row is rewritten whole, so the new pair is what counts; the row's own id is ignored, so a channel that keeps its pair does not
    conflict with itself (the body ``id`` equals the path ``id`` by the time a hook runs).
    """
    provider = await _stored_provider(entity, storage_provider)
    _check_platform_matches(entity, provider)
    await _refuse_if_pair_taken(entity, storage_provider, ignore_id=entity.id)


__all__ = ["check_channel_on_create", "check_channel_on_update"]
