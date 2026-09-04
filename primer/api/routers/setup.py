"""Setup router: explicit seeding and the operator/builder reset.

Both endpoints are admin-only. ``/setup/seed`` is what the first-run
wizard calls the moment it has created a model profile, which is the
amendment C3 ordering fix: seeding cannot happen before a profile exists,
so completion re-runs the pass rather than waiting for the next boot.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from primer.api.deps import (
    get_llm_provider_storage,
    get_provider_registry,
    get_semantic_search_registry,
    get_storage_provider,
    get_workspace_registry,
)
from primer.api.errors import common_responses
from primer.bootstrap.defaults import (
    RESERVED_BUILDER_AGENT,
    RESERVED_DEFAULT_WORKSPACE,
    RESERVED_EXPLORER_AGENT,
    RESERVED_OPERATOR_AGENT,
    RESERVED_PLANNER_AGENT,
    RESERVED_TOOL_RUNNER_AGENT,
)
from primer.bootstrap.operator_defaults import (
    builder_agent,
    explorer_agent,
    operator_agent,
    planner_agent,
    tool_runner_agent,
)
from primer.bootstrap.seed import default_profile_id, run_ensure_pass
from primer.bootstrap.setup_state import (
    MISSING_BUILDER_AGENT,
    MISSING_DEFAULT_WORKSPACE,
    MISSING_LLM_PROVIDER,
    MISSING_MODEL_PROFILE,
    MISSING_OPERATOR_AGENT,
    MISSING_SYSTEM_COLLECTION,
    evaluate_setup_state,
)
from primer.model.agent import Agent
from primer.model.except_ import BadRequestError, ConfigError, NotFoundError
from primer.model.provider import LLMProvider
from primer.model.storage import OffsetPage

# Sibling router import: reuses the EXISTING public live-probe endpoint
# (already used by the provider console's "discovered models" panel)
# rather than re-implementing per-provider-type network calls here.
from primer.api.routers.providers import discover_saved_llm_models


logger = logging.getLogger(__name__)

setup_router = APIRouter(prefix="/setup", tags=["setup"])


@setup_router.post(
    "/seed",
    summary="Run the S5 ensure pass now",
    responses=common_responses(500),
)
async def seed_now(request: Request) -> dict:
    """Idempotently ensure the seeded world, then report setup state."""
    storage_provider = get_storage_provider(request)
    toolsets = {
        name: getattr(request.app.state, f"{name}_toolset")
        for name in ("system", "crud")
        if getattr(request.app.state, f"{name}_toolset", None) is not None
    }
    result = await run_ensure_pass(
        storage_provider,
        workspace_registry=get_workspace_registry(request),
        toolset_providers=toolsets,
        provider_registry=get_provider_registry(request),
        semantic_search_registry=get_semantic_search_registry(request),
    )
    state = await evaluate_setup_state(storage_provider)
    return {
        "created": result.created,
        "skipped": result.skipped,
        "errors": [list(pair) for pair in result.errors],
        "setup_complete": state.complete,
        "setup_missing": state.missing,
    }


@setup_router.post(
    "/reset_agents",
    summary="Restore the default operator and builder definitions",
    # ConfigError maps to 503 (primer/api/errors.py:71), not 409; the
    # response map has to name the code the handler can actually produce.
    responses=common_responses(500, 503),
)
async def reset_agents(request: Request) -> dict:
    """Re-apply the DEFAULT prompt, grants and description of both agents.

    Explicitly overwrites exactly those two rows and nothing else. The
    model profile each row runs under is PRESERVED: repointing the
    operator at a better model is a configuration choice, not a drift.
    """
    storage_provider = get_storage_provider(request)
    agents = storage_provider.get_storage(Agent)
    fallback = await default_profile_id(storage_provider)
    reset: list[str] = []
    for agent_id, factory in (
        (RESERVED_OPERATOR_AGENT, operator_agent),
        (RESERVED_BUILDER_AGENT, builder_agent),
        (RESERVED_PLANNER_AGENT, planner_agent),
        (RESERVED_EXPLORER_AGENT, explorer_agent),
        (RESERVED_TOOL_RUNNER_AGENT, tool_runner_agent),
    ):
        existing = await agents.get(agent_id)
        profile_id = (
            existing.model.profile_id if existing is not None else fallback
        )
        if profile_id is None:
            raise ConfigError(
                "cannot reset the seeded agents: no model profile exists "
                "(finish first-run setup first)"
            )
        default_row = factory(profile_id)
        if existing is None:
            await agents.create(default_row)
        else:
            await agents.update(
                existing.model_copy(
                    update={
                        "description": default_row.description,
                        "system_prompt": default_row.system_prompt,
                        "tools": default_row.tools,
                    }
                )
            )
        reset.append(agent_id)
    logger.info("setup: reset seeded agents %r", reset)
    return {"reset": reset}


# ---------------------------------------------------------------------------
# Live predicate state (R5: uiv2 notes section 4/5's "six live predicates")
#
# evaluate_setup_state() above is presence-only for all six rows - cheap,
# no network calls, safe to run on every GET /auth/status. Two of the six
# (llm_provider, default_workspace) the notes describe as LIVE checks
# ("the provider responds", "reachable"), not just "the row exists"
# (docs/superpowers/uiv2/03-backend-gap-map.md:162, BACKEND-GAP size S
# per predicate). Doing a real network/backend call on every auth-status
# poll would be slow and, for the LLM probe, needless load on the
# upstream - so the live checks live here instead, behind their own
# admin-gated endpoint the Setup page and first-boot wizard call
# explicitly, never the hot auth-status path.
# ---------------------------------------------------------------------------

_PREDICATE_LABELS: dict[str, str] = {
    MISSING_LLM_PROVIDER: "LLM provider responds",
    MISSING_MODEL_PROFILE: "Model profile registered",
    MISSING_DEFAULT_WORKSPACE: "Default workspace reachable",
    MISSING_OPERATOR_AGENT: "Operator agent seeded",
    MISSING_BUILDER_AGENT: "Builder agent seeded",
    MISSING_SYSTEM_COLLECTION: "System collection exists",
}

# Wizard order, matching AuthStatus.setup_missing's documented ordering
# (primer/api/routers/auth.py) so the two surfaces read the same list
# the same way.
_PREDICATE_ORDER: list[str] = [
    MISSING_LLM_PROVIDER,
    MISSING_MODEL_PROFILE,
    MISSING_DEFAULT_WORKSPACE,
    MISSING_OPERATOR_AGENT,
    MISSING_BUILDER_AGENT,
    MISSING_SYSTEM_COLLECTION,
]


class SetupPredicateOut(BaseModel):
    key: str
    label: str
    ok: bool
    live: bool = Field(
        ...,
        description=(
            "True when this predicate was actually probed live (a "
            "network or backend call), not just checked for row "
            "presence. False on the 4 presence-only predicates, and on "
            "llm_provider/default_workspace when their row is missing "
            "(nothing to probe yet)."
        ),
    )
    detail: str | None = Field(
        default=None,
        description="Failure reason when ok is False; unset when ok.",
    )


class SetupStateResponse(BaseModel):
    complete: bool
    missing: list[str]
    predicates: list[SetupPredicateOut]


async def _live_llm_provider_check(storage_provider) -> tuple[bool, str | None]:
    """Probe the first configured LLMProvider row for a live response.

    Reuses ``discover_saved_llm_models`` (the same live list-models probe
    the provider console's detail page already calls) rather than
    duplicating per-provider-type network dispatch here. Checks only the
    first row when several are configured - a lightweight ping, not an
    exhaustive sweep.
    """
    page = await storage_provider.get_storage(LLMProvider).list(
        OffsetPage(offset=0, length=1)
    )
    if not page.items:
        return False, "no LLM provider configured"
    row = page.items[0]
    try:
        await discover_saved_llm_models(
            provider_id=row.id,
            providers=get_llm_provider_storage(storage_provider),
        )
    except BadRequestError as exc:
        if "live model discovery is not supported" in str(exc):
            # 01a06918: unreachable today -- every current LLMProviderType
            # member has a live probe (_probe_llm_models' dispatch, see
            # its own comment on the analogous else branch). The old
            # reachable case was "aggregated" (a provider type with no
            # probe of its own); it moved off LLMProvider entirely onto
            # ModelProfile (01a067c4). Kept as a forward-looking fallback
            # for the same reason _probe_llm_models keeps its match: a
            # future provider type added without a live-probe path should
            # read as "configured, no live signal either way" here rather
            # than fail the setup gate outright.
            return True, None
        return False, str(exc)
    return True, None


async def _live_default_workspace_check(request: Request) -> tuple[bool, str | None]:
    """Re-attach the default workspace via its backend.

    Reuses ``WorkspaceRegistry.get_workspace`` - the same re-attach path
    every session start already depends on - rather than writing a new
    backend-specific stat. Any failure (missing row, dead
    container/pod, unreachable filesystem) reads as "not reachable".
    """
    workspace_registry = get_workspace_registry(request)
    try:
        await workspace_registry.get_workspace(RESERVED_DEFAULT_WORKSPACE)
    except NotFoundError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001 - any backend failure means "not reachable"
        return False, f"{type(exc).__name__}: {exc}"
    return True, None


@setup_router.get(
    "/state",
    response_model=SetupStateResponse,
    summary="Live-evaluate all six setup predicates",
    responses=common_responses(500),
)
async def get_setup_state(request: Request) -> SetupStateResponse:
    """The Setup page's six-predicate table and the first-boot wizard's
    gate both read this - not GET /auth/status, which stays presence-only
    for its hot, unauthenticated-probe role."""
    storage_provider = get_storage_provider(request)
    presence = await evaluate_setup_state(storage_provider)
    presence_missing = set(presence.missing)

    predicates: list[SetupPredicateOut] = []
    missing: list[str] = []
    for key in _PREDICATE_ORDER:
        if key == MISSING_LLM_PROVIDER:
            if key in presence_missing:
                ok, detail, live = False, "no LLM provider configured", False
            else:
                ok, detail = await _live_llm_provider_check(storage_provider)
                live = True
        elif key == MISSING_DEFAULT_WORKSPACE:
            if key in presence_missing:
                ok, detail, live = (
                    False, "default workspace row does not exist", False,
                )
            else:
                ok, detail = await _live_default_workspace_check(request)
                live = True
        else:
            ok = key not in presence_missing
            detail = None if ok else "row does not exist"
            live = False
        predicates.append(SetupPredicateOut(
            key=key, label=_PREDICATE_LABELS[key], ok=ok, live=live, detail=detail,
        ))
        if not ok:
            missing.append(key)

    return SetupStateResponse(
        complete=not missing, missing=missing, predicates=predicates,
    )


__all__ = ["setup_router"]
