"""Migration 2: replace ``LLMProvider.models[]`` with ``ModelProfile`` rows.

Three steps, each idempotent:

1. Synthesise one :class:`~primer.model.model_profile.ModelProfile` per
   legacy ``models[]`` entry.
2. Rewrite every ``Agent.model`` from ``{provider_id, model_name}`` to
   ``{profile_id}``.
3. Strip ``models[]`` from the provider rows.

**Why this module redefines LLMProvider and Agent.** A migration reads rows
written by the OLD schema, but it runs inside a build where the models have
already changed: ``LLMProvider.models`` no longer exists and
``AgentModel.profile_id`` is required. Loading those rows through the live
models would silently drop ``models[]`` in one case and fail validation in
the other.

The storage layer derives a table name from ``model_class.__name__.lower()``,
so a class named ``LLMProvider`` declared here addresses the same
``llmprovider`` table. Both views set ``extra="allow"`` so every field this
migration does not care about survives the read-modify-write untouched.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from primer.int.storage_provider import StorageProvider
from primer.model.common import Identifiable
from primer.model.model_profile import ModelProfile
from primer.model.storage import OffsetPage

logger = logging.getLogger(__name__)

_PAGE = 200
_UNSAFE = re.compile(r"[^a-z0-9-]+")


def synth_profile_id(provider_id: str, model_name: str) -> str:
    """Deterministic id for a migrated profile: ``<provider>--<model-slug>``.

    Deterministic so re-running the migration converges on the same ids
    rather than duplicating profiles, and so step 2 can compute the id an
    agent should point at without a lookup table.
    """
    slug = _UNSAFE.sub("-", model_name.lower()).strip("-")
    return f"{provider_id}--{slug}"


class LLMProvider(Identifiable):  # noqa: N801 - name selects the storage table
    """Migration-local view of a provider row, retaining ``models[]``."""

    model_config = ConfigDict(extra="allow")

    models: list[dict[str, Any]] | None = Field(default=None)


class Agent(Identifiable):  # noqa: N801 - name selects the storage table
    """Migration-local view of an agent row, with an untyped model block."""

    model_config = ConfigDict(extra="allow", protected_namespaces=())

    model: dict[str, Any] = Field(default_factory=dict)


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


class M002ModelProfiles:
    """Cut LLM model identity over from names to profile ids."""

    version = 2
    description = "replace LLMProvider.models[] with ModelProfile rows"

    async def apply(self, sp: StorageProvider) -> None:
        await self._synthesise_profiles(sp)
        await self._rewrite_agents(sp)
        await self._strip_models(sp)

    # -- step 1 ----------------------------------------------------------
    async def _synthesise_profiles(self, sp: StorageProvider) -> None:
        providers = sp.get_storage(LLMProvider)
        profiles = sp.get_storage(ModelProfile)
        created = 0
        async for row in _iter_rows(providers, LLMProvider):
            for entry in row.models or []:
                name = entry.get("name")
                if not name:
                    continue
                profile_id = synth_profile_id(row.id, name)
                if await profiles.get(profile_id) is not None:
                    continue
                await profiles.create(
                    ModelProfile(
                        id=profile_id,
                        description=(
                            f"Migrated from provider {row.id!r} model {name!r}."
                        ),
                        provider_id=row.id,
                        model_name=name,
                        context_length=entry.get("context_length") or 8192,
                    )
                )
                created += 1
        if created:
            logger.info("synthesised model profiles", extra={"count": created})

    # -- step 2 ----------------------------------------------------------
    async def _rewrite_agents(self, sp: StorageProvider) -> None:
        agents = sp.get_storage(Agent)
        profiles = sp.get_storage(ModelProfile)
        rewritten = 0
        async for row in _iter_rows(agents, Agent):
            block = row.model or {}
            if block.get("profile_id"):
                continue  # already migrated
            provider_id = block.get("provider_id")
            model_name = block.get("model_name")
            if not provider_id or not model_name:
                logger.warning(
                    "agent has no resolvable model block; leaving untouched",
                    extra={"agent_id": row.id},
                )
                continue
            profile_id = synth_profile_id(provider_id, model_name)
            if await profiles.get(profile_id) is None:
                # The provider row is gone, so no profile was synthesised.
                # The agent was already broken; do not invent a target.
                logger.warning(
                    "agent references a model with no matching provider; "
                    "leaving untouched",
                    extra={
                        "agent_id": row.id,
                        "provider_id": provider_id,
                        "model_name": model_name,
                    },
                )
                continue
            await agents.update(row.model_copy(update={"model": {"profile_id": profile_id}}))
            rewritten += 1
        if rewritten:
            logger.info("rewrote agent model bindings", extra={"count": rewritten})

    # -- step 3 ----------------------------------------------------------
    async def _strip_models(self, sp: StorageProvider) -> None:
        providers = sp.get_storage(LLMProvider)
        stripped = 0
        async for row in _iter_rows(providers, LLMProvider):
            if row.models is None:
                continue
            await providers.update(row.model_copy(update={"models": None}))
            stripped += 1
        if stripped:
            logger.info("stripped legacy models[]", extra={"count": stripped})


__all__ = ["M002ModelProfiles", "synth_profile_id"]
