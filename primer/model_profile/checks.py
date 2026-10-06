"""Pre-write checks for :class:`~primer.model.model_profile.ModelProfile`, shared by the REST router and the system tools (task
01a111d1, D5 phase 2b).

Moved out of ``primer/api/routers/model_profiles.py``, where they were bound to a ``Request``: one function per check over
``(entity, storage_provider)`` raising :class:`~primer.common.entity_checks.EntityCheckError` with the router's own ``code`` and
``field``. The router's hooks re-raise the exact ``HTTPException(422, detail={error, field, message})`` they always raised; the
system ``create_`` / ``update_model_profile`` tools answer ``validation-error``.

The three checks (see the router's module docstring for the aggregation directive):

* a single profile's ``provider_id`` names a stored LLMProvider;
* an aggregated profile names two or more distinct, existing, single members, and not itself;
* on an update, a profile another aggregate lists as a member cannot become an aggregate itself (nested aggregation is not
  supported).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from primer.common.entity_checks import EntityCheckError
from primer.model.model_profile import ModelProfile
from primer.model.provider import LLMProvider
from primer.model.storage import FieldRef, OffsetPage, Op, Predicate, Value

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


async def check_not_a_member_becoming_aggregated(entity: ModelProfile, *, storage_provider: "StorageProvider") -> None:
    """An UPDATE must not turn a profile into ``kind="aggregated"`` while some OTHER aggregate lists it as a member.

    ``check_aggregation_valid`` only validates an aggregate's OWN members at ITS OWN write time. Without this check, writing member
    profile A as an aggregate passes every other check and silently leaves the containing aggregate G in violation of "every member
    must be single", discovered only at resolve time with an error that misattributes the problem to G instead of A. Same CONTAINS
    lookup the reference block uses to stop deleting a member out from under its aggregate.
    """
    if entity.kind != "aggregated":
        return
    storage = storage_provider.get_storage(ModelProfile)
    predicate = Predicate(
        left=FieldRef(name="members"), op=Op.CONTAINS, right=Value(value=entity.id),
    )
    page = await storage.find(predicate, OffsetPage(offset=0, length=1))
    if page.items:
        raise _refuse(
            "member_of_another_aggregate",
            "kind",
            f"profile {entity.id!r} is a member of aggregate "
            f"{page.items[0].id!r} and cannot become kind='aggregated' "
            "itself (nested aggregation is not supported); remove it "
            "from that aggregate's members first",
        )


async def check_profile_on_create(entity: ModelProfile, *, storage_provider: "StorageProvider") -> None:
    """Every create-time check, in the router's order."""
    await check_provider_exists(entity, storage_provider=storage_provider)
    await check_aggregation_valid(entity, storage_provider=storage_provider)


async def check_profile_on_update(entity: ModelProfile, *, storage_provider: "StorageProvider") -> None:
    """Every update-time check, in the router's order (the checks validate the incoming shape only)."""
    await check_provider_exists(entity, storage_provider=storage_provider)
    await check_aggregation_valid(entity, storage_provider=storage_provider)
    await check_not_a_member_becoming_aggregated(entity, storage_provider=storage_provider)


__all__ = [
    "check_aggregation_valid",
    "check_not_a_member_becoming_aggregated",
    "check_profile_on_create",
    "check_profile_on_update",
    "check_provider_exists",
]
