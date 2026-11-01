"""Pre-write checks for :class:`~primer.model.model_profile.ModelProfile`, shared by the REST router and the system tools (task
01a111d1, D5 phase 2b).

Moved out of ``primer/api/routers/model_profiles.py``, where they were bound to a ``Request``: one function per check over
``(entity, storage_provider)`` raising :class:`~primer.common.entity_checks.EntityCheckError` with the router's own ``code`` and
``field``. The router's hooks re-raise the exact ``HTTPException(422, detail={error, field, message})`` they always raised; the
system ``create_`` / ``update_model_profile`` tools answer ``validation-error``.

The checks (see the router's module docstring for the aggregation directive):

* a single profile's ``provider_id`` names a stored LLMProvider;
* an aggregated profile names two or more distinct, existing, single members, and not itself;
* on an update, the profile's kind is unchanged (GX-16: single and aggregated are not interconvertible, which is also why a listed member can
  never become an aggregate).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from primer.common.entity_checks import EntityCheckError
from primer.model.model_profile import ModelProfile
from primer.model.provider import LLMProvider

if TYPE_CHECKING:
    from primer.int.storage_provider import StorageProvider


def _refuse(code: str, field: str, message: str) -> EntityCheckError:
    return EntityCheckError("validation", message, code=code, field=field)


async def check_provider_exists(entity: ModelProfile, *, storage_provider: "StorageProvider") -> None:
    """A single profile's ``provider_id`` must name a stored LLMProvider (an aggregated profile has none of its own)."""
    if entity.kind != "single":
        return
    storage = storage_provider.get_storage(LLMProvider)
    if await storage.get(entity.provider_id) is None:
        raise _refuse(
            "provider_not_found",
            "provider_id",
            f"LLMProvider {entity.provider_id!r} does not exist; "
            "create the provider before registering a profile on it",
        )


async def check_aggregation_valid(entity: ModelProfile, *, storage_provider: "StorageProvider") -> None:
    """An aggregated profile's ``members`` must satisfy the aggregation invariants. No-op for ``kind="single"``."""
    if entity.kind != "aggregated":
        return
    members = entity.members or []
    if len(members) < 2:
        raise _refuse(
            "aggregation_too_small",
            "members",
            "an aggregated profile must name at least two member "
            "profiles, per the aggregation directive: \"an aggregated "
            "profile is an aggregation of two or more model profiles\"",
        )
    if entity.id in members:
        raise _refuse(
            "self_reference",
            "members",
            f"profile {entity.id!r} cannot name itself as a member",
        )
    if len(set(members)) != len(members):
        raise _refuse(
            "duplicate_member",
            "members",
            "members must not contain duplicates; order is the "
            "routing/failover chain, so a duplicate would silently "
            "change behaviour rather than being a harmless repeat",
        )
    storage = storage_provider.get_storage(ModelProfile)
    for member_id in members:
        member = await storage.get(member_id)
        if member is None:
            raise _refuse(
                "member_not_found",
                "members",
                f"member profile {member_id!r} does not exist",
            )
        if member.kind != "single":
            raise _refuse(
                "nested_aggregation",
                "members",
                f"member profile {member_id!r} is itself "
                "kind='aggregated'; nested aggregation is not "
                "supported (v1)",
            )


def check_kind_unchanged(entity: ModelProfile, existing: ModelProfile) -> None:
    """An UPDATE must keep the stored profile's ``kind``: single and aggregated profiles are not interconvertible (GX-16).

    Fixing the kind at creation also removes a race the old "a listed member may not become an aggregate" check could not close (one admin adds P
    to an aggregate while another turns P into one: two unfenced reads); with the kind fixed, a member (always single) stays single.
    """
    if entity.kind != existing.kind:
        raise _refuse(
            "profile_kind_immutable",
            "kind",
            f"profile {entity.id!r} is kind={existing.kind!r} and cannot become kind={entity.kind!r}: a profile's kind is fixed when it is "
            "created; create a new profile instead",
        )


async def check_profile_on_create(entity: ModelProfile, *, storage_provider: "StorageProvider") -> None:
    """Every create-time check, in the router's order."""
    await check_provider_exists(entity, storage_provider=storage_provider)
    await check_aggregation_valid(entity, storage_provider=storage_provider)


async def check_profile_on_update(entity: ModelProfile, existing: ModelProfile, *, storage_provider: "StorageProvider") -> None:
    """Every update-time check, in order: the kind is unchanged, then the incoming shape is valid. Both write doors (the REST router and the
    system tools) run exactly this."""
    check_kind_unchanged(entity, existing)
    await check_provider_exists(entity, storage_provider=storage_provider)
    await check_aggregation_valid(entity, storage_provider=storage_provider)


__all__ = [
    "check_aggregation_valid",
    "check_kind_unchanged",
    "check_profile_on_create",
    "check_profile_on_update",
    "check_provider_exists",
]
