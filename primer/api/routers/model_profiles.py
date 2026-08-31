"""ModelProfile CRUD router.

A profile names one ``(provider, model)`` pair plus its API-level config,
and the profile id is what an :class:`~primer.model.agent.Agent`
references. Standard CRUD + Find from :mod:`primer.api.routers._crud`,
plus three additions:

* ``provider_id`` must resolve to an existing :class:`LLMProvider`,
  checked on create and update. Without this an operator can persist a
  profile that fails only at turn time, which is a much worse place to
  discover a typo.
* Deleting a profile an Agent still references returns 409 rather than
  silently breaking that agent.
* The list route enriches each item with ``agent_count`` /
  ``graph_node_count`` (platform wave P2, #19) so a profile card can
  render "bound by N agents" from one fetch.
"""

from __future__ import annotations

from collections import Counter

from fastapi import Depends, HTTPException, Request
from pydantic import Field

from primer.api.deps import (
    get_agent_storage,
    get_llm_provider_storage,
    get_model_profile_storage,
)
from primer.api.routers._crud import make_crud_router
from primer.api.routers._references import ReferenceCheck
from primer.model.agent import Agent
from primer.model.graph import Graph
from primer.model.model_profile import ModelProfile
from primer.model.provider import LLMProvider
from primer.model.storage import CursorPageResponse, OffsetPage, OffsetPageResponse

_REF_COUNT_PAGE_SIZE = 200


class ModelProfileWithUsage(ModelProfile):
    """Response-only shape: a :class:`ModelProfile` plus reference counts.

    Never persisted -- ``agent_count``/``graph_node_count`` are computed
    fresh on every ``GET /model_profiles`` (see ``_enrich_with_usage``
    below), not stored fields, so they carry no MIGRATIONS implication
    (nothing about ModelProfile's own Storage[T] shape changes).

    Distinguishing "bound" from "unbound" is exactly ``agent_count == 0
    and graph_node_count == 0`` -- zero is itself the honest, meaningful
    "unbound" answer, not a placeholder for missing data (unlike a
    provider's probe state, which needs a real tri-state; see #4/#5).
    """

    agent_count: int = Field(
        default=0,
        description=(
            "Number of Agent rows whose model.profile_id names this "
            "profile as their DEFAULT. Does not include graph nodes "
            "that override the profile per-node -- see graph_node_count."
        ),
    )
    graph_node_count: int = Field(
        default=0,
        description=(
            "Number of agent-kind Graph nodes across all graphs whose "
            "own profile_id override names this profile. A node with "
            "no override (profile_id is None) inherits its agent's "
            "default and is counted in agent_count instead, via that "
            "agent, not here."
        ),
    )


async def _enrich_with_usage(
    resp: OffsetPageResponse | CursorPageResponse, request: Request,
) -> OffsetPageResponse | CursorPageResponse:
    """Attach agent/graph-node reference counts to one page of profiles.

    Two full-table passes (Agent, Graph), each capped and paged at
    ``_REF_COUNT_PAGE_SIZE`` rows, tallied in memory -- NOT one query
    per profile in the current page. A page of profiles is typically
    far smaller than the Agent/Graph tables, so this trades "always two
    bounded scans" for "never N+1", which is the right side of that
    trade at list-route scale (mirrors primer.api.routers.providers's
    _catalogue_tools user-toolset paging).
    """
    storage_provider = request.app.state.storage_provider
    agent_storage = storage_provider.get_storage(Agent)
    graph_storage = storage_provider.get_storage(Graph)

    agent_counts: Counter[str] = Counter()
    offset = 0
    while True:
        page = await agent_storage.list(
            OffsetPage(offset=offset, length=_REF_COUNT_PAGE_SIZE),
        )
        for agent in page.items:
            agent_counts[agent.model.profile_id] += 1
        if len(page.items) < _REF_COUNT_PAGE_SIZE:
            break
        offset += _REF_COUNT_PAGE_SIZE

    graph_node_counts: Counter[str] = Counter()
    offset = 0
    while True:
        page = await graph_storage.list(
            OffsetPage(offset=offset, length=_REF_COUNT_PAGE_SIZE),
        )
        for graph in page.items:
            for node in graph.nodes:
                profile_id = getattr(node, "profile_id", None)
                if profile_id:
                    graph_node_counts[profile_id] += 1
        if len(page.items) < _REF_COUNT_PAGE_SIZE:
            break
        offset += _REF_COUNT_PAGE_SIZE

    enriched = [
        ModelProfileWithUsage(
            **item.model_dump(),
            agent_count=agent_counts.get(item.id, 0),
            graph_node_count=graph_node_counts.get(item.id, 0),
        )
        for item in resp.items
    ]
    return resp.model_copy(update={"items": enriched})


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
    enrich_list=_enrich_with_usage,
)


__all__ = ["ModelProfileWithUsage", "model_profile_router"]
