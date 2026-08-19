"""Setup router: explicit seeding and the operator/builder reset.

Both endpoints are admin-only. ``/setup/seed`` is what the first-run
wizard calls the moment it has created a model profile, which is the
amendment C3 ordering fix: seeding cannot happen before a profile exists,
so completion re-runs the pass rather than waiting for the next boot.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from primer.api.deps import get_storage_provider, get_workspace_registry
from primer.api.errors import common_responses
from primer.bootstrap.defaults import (
    RESERVED_BUILDER_AGENT,
    RESERVED_OPERATOR_AGENT,
)
from primer.bootstrap.operator_defaults import builder_agent, operator_agent
from primer.bootstrap.seed import default_profile_id, run_ensure_pass
from primer.bootstrap.setup_state import evaluate_setup_state
from primer.model.agent import Agent
from primer.model.except_ import ConfigError


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


__all__ = ["setup_router"]
