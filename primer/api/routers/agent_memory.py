"""Agent memory REST surface (agent memory Task 5): ``/v1/agents/{id}/memory/settings``.

Anyone who can read the agent reads its memory settings; only an owner (an admin included) changes them. The settings row is created on the first
PUT, so a GET before that answers the defaults (memory off).
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Request
from pydantic import ConfigDict, create_model

from primer.access.person import person_from_request
from primer.access.policy import level_of, require
from primer.api.deps import get_storage_provider
from primer.api.errors import common_responses
from primer.memory.prompts import DEFAULT_REFLECTION_PROMPT
from primer.memory.settings import EDITABLE, get_settings, save_settings
from primer.model.access import AccessLevel
from primer.model.agent import Agent
from primer.model.except_ import NotFoundError
from primer.model.memory import AgentMemory

MEMORY_OWNER_ONLY = "memory_owner_only"

#: The body of a PUT: the owner's fields with the row's own bounds and defaults, and nothing else (an unknown or system field is a 422).
MemorySettingsBody = create_model(
    "MemorySettingsBody",
    __config__=ConfigDict(extra="forbid"),
    **{name: (AgentMemory.model_fields[name].annotation, AgentMemory.model_fields[name]) for name in EDITABLE},
)


class MemorySettingsView(AgentMemory):
    """The settings as the console reads them, with the built-in reflection prompt it shows read-only."""

    default_reflection_prompt: str = DEFAULT_REFLECTION_PROMPT


agent_memory_router = APIRouter(tags=["agent-memory"])


async def _load(request: Request, agent_id: str) -> Agent:
    agent = await get_storage_provider(request).get_storage(Agent).get(agent_id)
    if agent is None:
        raise NotFoundError(f"Agent {agent_id!r} does not exist")
    return agent


@agent_memory_router.get(
    "/agents/{agent_id}/memory/settings",
    response_model=MemorySettingsView,
    summary="The agent's memory settings",
    responses=common_responses(404, 500),
)
async def get_memory_settings(request: Request, agent_id: str = Path(..., description="Agent id")) -> MemorySettingsView:
    agent = await _load(request, agent_id)
    require(person_from_request(request), agent.access, AccessLevel.READ, what=f"Agent {agent_id!r}")
    row = await get_settings(get_storage_provider(request), agent_id)
    return MemorySettingsView(**row.model_dump())


@agent_memory_router.put(
    "/agents/{agent_id}/memory/settings",
    response_model=MemorySettingsView,
    summary="Change the agent's memory settings (owners only)",
    responses=common_responses(403, 404, 409, 422, 500),
)
async def put_memory_settings(
    request: Request,
    agent_id: str = Path(..., description="Agent id"),
    body: MemorySettingsBody = Body(...),  # type: ignore[valid-type]
) -> MemorySettingsView:
    agent = await _load(request, agent_id)
    person = person_from_request(request)
    require(person, agent.access, AccessLevel.READ, what=f"Agent {agent_id!r}")  # no level at all: the agent does not exist to this person
    if level_of(person, agent.access) is not AccessLevel.OWNER:
        raise HTTPException(
            status_code=403,
            detail={"error": MEMORY_OWNER_ONLY, "message": f"only an owner of agent {agent_id!r} changes its memory settings"},
        )
    state = request.app.state
    registries = type("Registries", (), {
        "provider_registry": getattr(state, "provider_registry", None),
        "semantic_search_registry": getattr(state, "semantic_search_registry", None),
        "claim_engine": getattr(state, "claim_engine", None),
    })()
    saved = await save_settings(
        get_storage_provider(request), registries, agent, AgentMemory(id=agent_id, **body.model_dump()),
        actor=person,
    )
    return MemorySettingsView(**saved.model_dump())


__all__ = ["MEMORY_OWNER_ONLY", "MemorySettingsBody", "MemorySettingsView", "agent_memory_router"]
