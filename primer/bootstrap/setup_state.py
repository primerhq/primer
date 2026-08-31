"""Derived setup completeness (S5 spec section 3).

Setup is complete iff an LLMProvider row exists, a ModelProfile row
exists, and the seeded objects of spec section 4 exist. There is NO
stamp: every predicate is evaluated live, so deleting the operator (or
the last profile) honestly reopens the wizard. Auth-disabled mode is
unaffected because nothing here keys on users.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from primer.bootstrap.defaults import (
    RESERVED_BUILDER_AGENT,
    RESERVED_DEFAULT_WORKSPACE,
    RESERVED_OPERATOR_AGENT,
)
from primer.knowledge.system_collection import SYSTEM_COLLECTION_ID
from primer.model.agent import Agent
from primer.model.collection import Collection
from primer.model.model_profile import ModelProfile
from primer.model.provider import LLMProvider
from primer.model.storage import OffsetPage
from primer.model.workspace import Workspace


MISSING_LLM_PROVIDER = "llm_provider"
MISSING_MODEL_PROFILE = "model_profile"
MISSING_DEFAULT_WORKSPACE = "default_workspace"
MISSING_OPERATOR_AGENT = "operator_agent"
MISSING_BUILDER_AGENT = "builder_agent"
MISSING_SYSTEM_COLLECTION = "system_collection"


@dataclass
class SetupState:
    """Live answer to "is this install configured?"."""

    complete: bool
    missing: list[str] = field(default_factory=list)


async def _has_any(storage_provider, model_cls) -> bool:
    """True iff at least one row exists. Cheap: page of length 1."""
    page = await storage_provider.get_storage(model_cls).list(
        OffsetPage(offset=0, length=1)
    )
    return len(page.items) > 0


async def evaluate_setup_state(storage_provider) -> SetupState:
    """Evaluate every setup predicate against live storage."""
    missing: list[str] = []
    if not await _has_any(storage_provider, LLMProvider):
        missing.append(MISSING_LLM_PROVIDER)
    if not await _has_any(storage_provider, ModelProfile):
        missing.append(MISSING_MODEL_PROFILE)
    workspaces = storage_provider.get_storage(Workspace)
    if await workspaces.get(RESERVED_DEFAULT_WORKSPACE) is None:
        missing.append(MISSING_DEFAULT_WORKSPACE)
    agents = storage_provider.get_storage(Agent)
    if await agents.get(RESERVED_OPERATOR_AGENT) is None:
        missing.append(MISSING_OPERATOR_AGENT)
    if await agents.get(RESERVED_BUILDER_AGENT) is None:
        missing.append(MISSING_BUILDER_AGENT)
    collections = storage_provider.get_storage(Collection)
    if await collections.get(SYSTEM_COLLECTION_ID) is None:
        missing.append(MISSING_SYSTEM_COLLECTION)
    return SetupState(complete=not missing, missing=missing)


__all__ = [
    "MISSING_BUILDER_AGENT",
    "MISSING_DEFAULT_WORKSPACE",
    "MISSING_LLM_PROVIDER",
    "MISSING_MODEL_PROFILE",
    "MISSING_OPERATOR_AGENT",
    "MISSING_SYSTEM_COLLECTION",
    "SetupState",
    "evaluate_setup_state",
]
