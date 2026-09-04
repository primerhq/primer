"""ModelProfile CRUD router.

A profile names one ``(provider, model)`` pair plus its API-level config
(``kind="single"``), OR is itself an ordered aggregation of two or more
other profiles (``kind="aggregated"``, no provider/model of its own) --
see :class:`primer.model.model_profile.ModelProfile`. The profile id is
what an :class:`~primer.model.agent.Agent` references either way.
Standard CRUD + Find from :mod:`primer.api.routers._crud`, plus four
additions:

* ``provider_id`` must resolve to an existing :class:`LLMProvider`,
  checked on create and update -- only for ``kind="single"``, since an
  aggregated profile has no ``provider_id`` of its own. Without this an
  operator can persist a profile that fails only at turn time, which is
  a much worse place to discover a typo.
* An aggregated profile's ``members`` are validated on create and
  update: at least two (the user's literal directive -- "an aggregated
  profile is an aggregation of two or more model profiles"), every
  member id must exist, every member must itself be ``kind="single"``
  (nested aggregation is REJECTED eagerly here, v1 -- closes the gap
  where the old ``AggregatedLLM`` only discovered a bad member lazily at
  resolve/stream time), no self-reference, and no duplicate member ids
  (order is the routing/failover chain, so silently deduping would
  silently change semantics -- reject instead of guessing).
* Deleting a profile an Agent still references, OR that is named as a
  member of any aggregated profile, returns 409 rather than silently
  breaking that agent or that aggregate.
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
from primer.model.storage import (
    CursorPageResponse,
    FieldRef,
    Op,
    OffsetPage,
    OffsetPageResponse,
    Predicate,
    Value,
)

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


async def _check_provider_exists(entity: ModelProfile, request: Request) -> None:
    """422 when ``provider_id`` names an LLMProvider that does not exist.

    Only meaningful for ``kind="single"`` -- an aggregated profile has no
    ``provider_id`` of its own (see :class:`ModelProfile`'s own
    kind-shape validator, which already guarantees it is None here).

    Uses 422 rather than 404: the request itself is well-formed, but a
    body field fails a semantic check, which is the same shape the CRUD
    factory uses for other reference validation.
    """
    if entity.kind != "single":
        return
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


def _aggregation_error(error: str, field: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"error": error, "field": field, "message": message},
    )


async def _check_aggregation_valid(entity: ModelProfile, request: Request) -> None:
    """422 when an aggregated profile's ``members`` violate the
    aggregation invariants (see module docstring's second bullet for the
    full list). No-op for ``kind="single"``.
    """
    if entity.kind != "aggregated":
        return
    members = entity.members or []
    if len(members) < 2:
        raise _aggregation_error(
            "aggregation_too_small",
            "members",
            "an aggregated profile must name at least two member "
            "profiles, per the aggregation directive: \"an aggregated "
            "profile is an aggregation of two or more model profiles\"",
        )
    if entity.id in members:
        raise _aggregation_error(
            "self_reference",
            "members",
            f"profile {entity.id!r} cannot name itself as a member",
        )
    if len(set(members)) != len(members):
        raise _aggregation_error(
            "duplicate_member",
            "members",
            "members must not contain duplicates; order is the "
            "routing/failover chain, so a duplicate would silently "
            "change behaviour rather than being a harmless repeat",
        )
    storage = request.app.state.storage_provider.get_storage(ModelProfile)
    for member_id in members:
        member = await storage.get(member_id)
        if member is None:
            raise _aggregation_error(
                "member_not_found",
                "members",
                f"member profile {member_id!r} does not exist",
            )
        if member.kind != "single":
            raise _aggregation_error(
                "nested_aggregation",
                "members",
                f"member profile {member_id!r} is itself "
                "kind='aggregated'; nested aggregation is not "
                "supported (v1)",
            )


async def _check_not_a_member_becoming_aggregated(
    entity: ModelProfile, request: Request,
) -> None:
    """422 when an UPDATE turns a profile into ``kind="aggregated"``
    while it is currently named as a member of some OTHER aggregate.

    _check_aggregation_valid only validates an aggregate's OWN members at
    ITS OWN write time -- it has no reason to re-check every aggregate
    that might reference THIS profile as a member. Without this guard,
    PUT'ing member profile A to kind="aggregated" passes every existing
    hook cleanly, silently leaving the containing aggregate G in
    violation of "every member must be kind=single" -- a nested
    aggregation G never agreed to, discovered only later at resolve time
    with an error that misattributes the problem to G instead of A.
    Same CONTAINS lookup ReferenceCheck already uses to block deleting a
    member out from under its aggregate (primer/api/routers/
    _references.py) -- this is that same relationship, checked on the
    OTHER kind of mutation (an in-place shape change, not a delete).
    """
    if entity.kind != "aggregated":
        return
    storage = request.app.state.storage_provider.get_storage(ModelProfile)
    predicate = Predicate(
        left=FieldRef(name="members"), op=Op.CONTAINS, right=Value(value=entity.id),
    )
    page = await storage.find(predicate, OffsetPage(offset=0, length=1))
    if page.items:
        raise _aggregation_error(
            "member_of_another_aggregate",
            "kind",
            f"profile {entity.id!r} is a member of aggregate "
            f"{page.items[0].id!r} and cannot become kind='aggregated' "
            "itself (nested aggregation is not supported); remove it "
            "from that aggregate's members first",
        )


async def _on_pre_create(entity: ModelProfile, request: Request) -> None:
    await _check_provider_exists(entity, request)
    await _check_aggregation_valid(entity, request)


async def _on_pre_update(
    entity: ModelProfile, existing: ModelProfile, request: Request
) -> None:
    del existing  # both checks validate the incoming shape only
    await _check_provider_exists(entity, request)
    await _check_aggregation_valid(entity, request)
    await _check_not_a_member_becoming_aggregated(entity, request)


def _agent_storage_from_request(request: Request):
    """Adapt the ``Storage[Agent]`` handle to the ReferenceCheck contract."""
    return request.app.state.storage_provider.get_storage(Agent)


def _model_profile_storage_from_request(request: Request):
    """Adapt the ``Storage[ModelProfile]`` handle to the ReferenceCheck
    contract -- self-referential: a profile can be a MEMBER of another
    profile, so the "child" kind here is ModelProfile itself.
    """
    return request.app.state.storage_provider.get_storage(ModelProfile)


model_profile_router = make_crud_router(
    model_cls=ModelProfile,
    storage_dep=get_model_profile_storage,
    plural="model_profiles",
    tag="model-profiles",
    managed_by_field="harness_id",
    search_fields=["id", "description", "model_name"],
    on_pre_create=_on_pre_create,
    on_pre_update=_on_pre_update,
    references=[
        ReferenceCheck(
            child_kind="agent",
            child_storage=_agent_storage_from_request,
            child_field="model.profile_id",
            error_code="in_use_by",
        ),
        ReferenceCheck(
            child_kind="model_profile (aggregate member)",
            child_storage=_model_profile_storage_from_request,
            child_field="members",
            op=Op.CONTAINS,
            error_code="in_use_by",
        ),
    ],
    enrich_list=_enrich_with_usage,
)


__all__ = ["ModelProfileWithUsage", "model_profile_router"]
