"""Deleting an LLM provider takes its model profiles with it, unless one of them is still in use (GS-5).

A provider creates its profiles, so a provider nobody uses must be deletable in one step. The delete is refused (409 ``in_use_by``, naming each
reference) only when at least one of its profiles is referenced: by an agent, by a graph node's ``profile_id`` override, or as a member of an aggregate
profile. (There is no stored default-profile setting: ``default_profile_id`` is just the first row, and the seeded agents that bound to it are agents.) The REST route and the system tool both call :func:`delete_llm_provider_profiles`.
"""

from __future__ import annotations

from typing import Any

from primer.model.agent import Agent
from primer.model.except_ import ConflictError
from primer.model.graph import Graph
from primer.model.model_profile import ModelProfile
from primer.model.storage import FieldRef, OffsetPage, Op, Predicate, Value
from primer.storage.references import first_graph_with_profile_override, first_referencing_row

_PAGE = 200


async def _profiles_of(storage_provider: Any, provider_id: str) -> list[ModelProfile]:
    storage = storage_provider.get_storage(ModelProfile)
    predicate = Predicate(left=FieldRef(name="provider_id"), op=Op.EQ, right=Value(value=provider_id))
    profiles: list[ModelProfile] = []
    while True:
        page = await storage.find(predicate, OffsetPage(offset=len(profiles), length=_PAGE))
        profiles.extend(page.items)
        if len(page.items) < _PAGE:
            return profiles


async def _uses_of(storage_provider: Any, profile_id: str) -> list[str]:
    """What still references one profile, as short phrases."""
    uses: list[str] = []
    agent = await first_referencing_row(
        storage_provider.get_storage(Agent), field="model.profile_id", op=Op.EQ, parent_id=profile_id,
    )
    if agent is not None:
        uses.append(f"agent {agent.id!r}")
    graph = await first_graph_with_profile_override(storage_provider.get_storage(Graph), profile_id)
    if graph is not None:
        uses.append(f"graph {graph.id!r} (node profile override)")
    aggregate = await first_referencing_row(
        storage_provider.get_storage(ModelProfile), field="members", op=Op.CONTAINS, parent_id=profile_id,
    )
    if aggregate is not None:
        uses.append(f"aggregate profile {aggregate.id!r}")
    return uses


async def delete_llm_provider_profiles(storage_provider: Any, provider_id: str) -> list[str]:
    """Delete every model profile of ``provider_id`` when none is referenced (returns their ids); else raise a 409 ``in_use_by`` listing the references."""
    profiles = await _profiles_of(storage_provider, provider_id)
    in_use = []
    for profile in profiles:
        uses = await _uses_of(storage_provider, profile.id)
        if uses:
            in_use.append(f"model_profile {profile.id!r}: {', '.join(uses)}")
    if in_use:
        raise ConflictError(f"in_use_by: LLM provider {provider_id!r} has profiles still in use ({'; '.join(in_use)})")
    storage = storage_provider.get_storage(ModelProfile)
    for profile in profiles:
        await storage.delete(profile.id)
    return [profile.id for profile in profiles]


__all__ = ["delete_llm_provider_profiles"]
