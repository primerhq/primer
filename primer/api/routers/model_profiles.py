"""ModelProfile CRUD router.

A profile names one ``(provider, model)`` pair plus its API-level config,
and the profile id is what an :class:`~primer.model.agent.Agent`
references. Standard CRUD + Find from :mod:`primer.api.routers._crud`,
plus two guards:

* ``provider_id`` must resolve to an existing :class:`LLMProvider`,
  checked on create and update. Without this an operator can persist a
  profile that fails only at turn time, which is a much worse place to
  discover a typo.
* Deleting a profile an Agent still references returns 409 rather than
  silently breaking that agent.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from primer.api.deps import (
    get_agent_storage,
    get_llm_provider_storage,
    get_model_profile_storage,
)
from primer.api.routers._crud import make_crud_router
from primer.api.routers._references import ReferenceCheck
from primer.model.model_profile import ModelProfile
from primer.model.provider import LLMProvider


async def _assert_provider_exists(entity: ModelProfile, request: Request) -> None:
    """422 when ``provider_id`` names an LLMProvider that does not exist.

    Uses 422 rather than 404: the request itself is well-formed, but a
    body field fails a semantic check, which is the same shape the CRUD
    factory uses for other reference validation.
    """
    storage = request.app.state.storage_provider.get_storage(LLMProvider)
    if await storage.get(entity.provider_id) is None:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "provider_not_found",
                "field": "provider_id",
                "message": (
                    f"LLMProvider {entity.provider_id!r} does not exist; "
                    "create the provider before registering a profile on it"
                ),
            },
        )


def _agent_storage_from_request(request: Request):
    """Adapt the ``Storage[Agent]`` handle to the ReferenceCheck contract."""
    from primer.model.agent import Agent

    return request.app.state.storage_provider.get_storage(Agent)


model_profile_router = make_crud_router(
    model_cls=ModelProfile,
    storage_dep=get_model_profile_storage,
    plural="model_profiles",
    tag="model-profiles",
    managed_by_field="harness_id",
    search_fields=["id", "description", "model_name"],
    on_pre_create=_assert_provider_exists,
    on_pre_update=_assert_provider_exists,
    references=[
        ReferenceCheck(
            child_kind="agent",
            child_storage=_agent_storage_from_request,
            child_field="model.profile_id",
            error_code="in_use_by",
        ),
    ],
)


__all__ = ["model_profile_router"]
