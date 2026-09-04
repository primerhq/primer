"""Migration 7: aggregated LLM providers become aggregated ModelProfiles.

The "aggregated" concept was modeled as its own ``LLMProvider`` kind
(``provider="aggregated"``, ``config`` an ordered member pool + routing/
failover policy). Per the user directive ("an aggregated profile is an
aggregation of two or more model profiles"), it is now a ``ModelProfile``
shape instead (``kind="aggregated"``) -- see
:class:`primer.model.model_profile.ModelProfile`.

Two steps, each idempotent:

1. For every ``ModelProfile`` whose ``provider_id`` names an aggregated
   ``LLMProvider``, convert it IN PLACE: for each of the old provider's
   ``config.members`` (``{provider_id, model_name}`` pairs), look up or
   synthesise the corresponding LEAF profile id via
   :func:`~primer.storage.migrations.m002_model_profiles.synth_profile_id`
   (the SAME deterministic id m002 already gave that pair, if the pair's
   own provider was ever migrated by m002), preserving member ORDER (it
   is the routing/failover chain). The profile keeps its own id, so
   nothing referencing it (an ``Agent.model.profile_id`` or a graph
   node's ``profile_id`` override) needs rewriting.
2. Delete the now-orphaned aggregated ``LLMProvider`` rows.

**Why this module redefines LLMProvider.** Mirrors m002's own reasoning:
this migration reads rows written by the OLD schema (``provider=
"aggregated"``, an ``AggregatedLLMConfig``-shaped ``config``), but runs
inside a build where ``LLMProviderType.AGGREGATED`` and
``AggregatedLLMConfig`` no longer exist on the live model at all --
loading such a row through the live ``LLMProvider`` would fail Pydantic
validation outright (unknown enum value, no matching config union arm).
The shadow class below reads ``provider``/``config`` untyped instead.
``ModelProfile`` itself is NOT shadowed: the live shape already reads an
old (pre-migration) row fine (no ``kind`` field on disk defaults to
``"single"``, which is exactly what every pre-migration profile is), and
the live shape is exactly what this migration needs to WRITE the new
aggregated rows in.

Live-DB fact (verified before this migration was written): zero
aggregated ``LLMProvider`` rows exist in production, so this migration's
real-world effect is a no-op; its correctness is exercised purely by
synthetic fixtures in ``tests/storage/test_m007_aggregated_model_profiles.py``.
That is also
why the min-members tightening (the old ``AggregatedLLMConfig.members``
allowed as few as 1; the new ``ModelProfile`` CRUD layer requires >= 2)
ships with no grandfather path -- there is nothing to grandfather.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ConfigDict, Field

from primer.int.storage_provider import StorageProvider
from primer.model.common import Identifiable
from primer.model.model_profile import (
    FailoverClasses,
    FailoverPoint,
    ModelProfile,
    RoutingStrategy,
)
from primer.model.storage import OffsetPage
from primer.storage.migrations.m002_model_profiles import synth_profile_id

logger = logging.getLogger(__name__)

_PAGE = 200

# Defensive fallback ONLY: every real member should already have a real
# ModelProfile with its own context_length (either from m002 or from
# ordinary CRUD), so this path is not expected to run outside a
# synthetic test fixture that deliberately omits it. Mirrors m002's own
# `entry.get("context_length") or 8192` fallback for the same reason.
_FALLBACK_CONTEXT_LENGTH = 8192


class LLMProvider(Identifiable):  # noqa: N801 - name selects the storage table
    """Migration-local view of a provider row: ``provider``/``config`` are
    read untyped since the live model no longer has an AGGREGATED arm.
    """

    model_config = ConfigDict(extra="allow")

    provider: str | None = Field(default=None)
    config: dict[str, Any] | None = Field(default=None)


async def _iter_rows(storage, model_cls):
    """Page through every row of one table."""
    offset = 0
    while True:
        page = await storage.list(OffsetPage(offset=offset, length=_PAGE))
        if not page.items:
            return
        for row in page.items:
            yield row
        if len(page.items) < _PAGE:
            return
        offset += _PAGE


class M007AggregatedModelProfiles:
    """Cut the aggregated LLM concept over from LLMProvider to ModelProfile."""

    version = 7
    description = "aggregated LLM providers become aggregated ModelProfiles"

    async def apply(self, sp: StorageProvider) -> None:
        aggregated_provider_ids, unconverted_provider_ids = (
            await self._convert_profiles(sp)
        )
        await self._delete_orphaned_providers(
            sp, aggregated_provider_ids, unconverted_provider_ids,
        )

    # -- step 1 ------------------------------------------------------------
    async def _convert_profiles(
        self, sp: StorageProvider,
    ) -> tuple[set[str], set[str]]:
        providers = sp.get_storage(LLMProvider)
        profiles = sp.get_storage(ModelProfile)

        aggregated_providers: dict[str, LLMProvider] = {}
        async for row in _iter_rows(providers, LLMProvider):
            if row.provider == "aggregated":
                aggregated_providers[row.id] = row
        if not aggregated_providers:
            return set(), set()

        # ALL aggregated provider ids are returned (not just ones a
        # profile was successfully converted for) -- every one of them
        # gets deleted in step 2 regardless, because after this
        # migration LLMProviderType.AGGREGATED no longer exists on the
        # live model at all, so a leftover row shaped that way becomes
        # permanently unreadable (fails Pydantic validation) the next
        # time anything lists LLMProvider rows generically.
        converted = 0
        unconverted_provider_ids: set[str] = set()
        async for profile in _iter_rows(profiles, ModelProfile):
            if profile.kind == "aggregated":
                continue  # already converted by a prior run
            provider = aggregated_providers.get(profile.provider_id or "")
            if provider is None:
                continue
            member_ids = await self._resolve_member_ids(profiles, provider)
            if len(member_ids) < 2:
                # The OLD schema allowed a 1-member aggregation
                # (AggregatedLLMConfig.members had min_length=1); the new
                # CRUD-time check requires >= 2. Rather than invent a
                # second member, leave this profile pointing at a
                # provider id that step 2 is about to delete -- broken,
                # loudly and diagnosably, matching m002's own precedent
                # ("the agent was already broken; do not invent a
                # target") rather than fabricating data. Recorded (not
                # just logged here) so step 2 can call this out loudly
                # too, at the point it actually orphans the profile.
                logger.warning(
                    "aggregated provider has fewer than two resolvable "
                    "members (old schema allowed 1; new requires >= 2); "
                    "leaving the profile UNCONVERTED -- it will end up "
                    "pointing at a provider id this migration deletes",
                    extra={"provider_id": provider.id, "profile_id": profile.id},
                )
                unconverted_provider_ids.add(provider.id)
                continue
            cfg = provider.config or {}
            # model_copy(update=...) does NOT run field validators/
            # coercion -- it assigns update's values verbatim. cfg's
            # values are plain strings read off an untyped dict, so they
            # must be coerced to the real enum types explicitly here, or
            # the written row would hold a str where a RoutingStrategy/
            # FailoverPoint/FailoverClasses is expected (StrEnum
            # equality happens to make `==` comparisons still pass, which
            # is exactly what let this slip past a first draft -- but
            # isinstance checks and JSON serialization would not).
            await profiles.update(
                profile.model_copy(
                    update={
                        "kind": "aggregated",
                        "members": member_ids,
                        "strategy": RoutingStrategy(
                            cfg.get("strategy", RoutingStrategy.SEQUENTIAL)
                        ),
                        "failover_point": FailoverPoint(
                            cfg.get("failover_point", FailoverPoint.BEFORE_FIRST_TOKEN)
                        ),
                        "failover_on": FailoverClasses(
                            cfg.get("failover_on", FailoverClasses.TRANSIENT_AND_CONFIG)
                        ),
                        "provider_id": None,
                        "model_name": None,
                        "context_length": None,
                    }
                )
            )
            converted += 1
        if converted:
            logger.info(
                "converted aggregated-provider-backed profiles to "
                "aggregated ModelProfiles",
                extra={"count": converted},
            )
        return set(aggregated_providers), unconverted_provider_ids

    async def _resolve_member_ids(
        self, profiles, provider: LLMProvider,
    ) -> list[str]:
        """Look up or synthesise a leaf ModelProfile id for each of the
        old provider's ``(provider_id, model_name)`` members, IN ORDER.
        """
        cfg = provider.config or {}
        member_ids: list[str] = []
        for member in cfg.get("members") or []:
            member_provider_id = member.get("provider_id")
            member_model_name = member.get("model_name")
            if not member_provider_id or not member_model_name:
                logger.warning(
                    "aggregated provider names a malformed member; skipping",
                    extra={"provider_id": provider.id, "member": member},
                )
                continue
            profile_id = synth_profile_id(member_provider_id, member_model_name)
            if await profiles.get(profile_id) is None:
                # m002 already ran for every real provider, so this is a
                # defensive path (a member naming a provider m002 never
                # saw, or a synthetic test fixture) -- see module
                # docstring on _FALLBACK_CONTEXT_LENGTH.
                await profiles.create(
                    ModelProfile(
                        id=profile_id,
                        description=(
                            f"Migrated aggregation member from provider "
                            f"{member_provider_id!r} model "
                            f"{member_model_name!r}."
                        ),
                        provider_id=member_provider_id,
                        model_name=member_model_name,
                        context_length=_FALLBACK_CONTEXT_LENGTH,
                    )
                )
                logger.warning(
                    "synthesised a leaf profile for an aggregation member "
                    "m002 never created (context_length is a fallback, "
                    "not the model's real limit)",
                    extra={"profile_id": profile_id},
                )
            member_ids.append(profile_id)
        return member_ids

    # -- step 2 --------------------------------------------------------
    async def _delete_orphaned_providers(
        self,
        sp: StorageProvider,
        provider_ids: set[str],
        unconverted_provider_ids: set[str],
    ) -> None:
        if not provider_ids:
            return
        providers = sp.get_storage(LLMProvider)
        deleted = 0
        deleted_unconverted: list[str] = []
        for provider_id in provider_ids:
            if await providers.get(provider_id) is None:
                continue  # already deleted by a prior run
            await providers.delete(provider_id)
            deleted += 1
            if provider_id in unconverted_provider_ids:
                deleted_unconverted.append(provider_id)
        if deleted_unconverted:
            # The "defensive, should be impossible under the old schema"
            # case from step 1 actually happened: at least one profile
            # is now left pointing at a provider id that no longer
            # exists. Logged at ERROR (not the step-1 WARNING) because
            # this is the point the orphaning becomes real, and it needs
            # to be findable in migration output, not just inferred from
            # a step-1 warning several log lines earlier.
            logger.error(
                "deleted aggregated LLMProvider rows whose profile could "
                "NOT be converted (fewer than 2 resolvable members) -- "
                "those profiles now have a dangling provider_id and need "
                "manual repair",
                extra={
                    "count": len(deleted_unconverted),
                    "provider_ids": deleted_unconverted,
                },
            )
        if deleted:
            logger.info(
                "deleted orphaned aggregated LLMProvider rows",
                extra={"count": deleted},
            )


__all__ = ["M007AggregatedModelProfiles"]
