"""S5 ensure pass: the idempotent, marker-independent seeding of the
agent-first world.

Unlike :class:`primer.bootstrap.runner.BootstrapRunner` (which stamps
``system_state.bootstrap_completed_at`` and skips itself afterwards), this
pass runs at EVERY startup and is additionally invoked by the setup
wizard's completion step. Ensure means create-when-absent: a row an
operator has edited is never overwritten, and a row that went missing is
repaired on the next boot.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import logging

from primer.model.except_ import ConflictError
from primer.model.tool_approval import (
    RequiredApprovalConfig,
    ToolApprovalPolicy,
)
from primer.toolset.crud import CRUD_TOOL_NAMES, CRUD_TOOLSET_ID


logger = logging.getLogger(__name__)


def crud_policy_id(tool_name: str) -> str:
    """Deterministic id for the default approval policy of a crud tool."""
    return f"tool-approval-policy-crud-{tool_name.replace('_', '-')}"


async def ensure_crud_approval_policies(storage_provider) -> list[str]:
    """Ensure one ``required`` approval policy per crud tool.

    Platform mutations are approval-gated by default; the approval system
    is the safety net that makes a user-editable builder agent safe. The
    rows are ordinary storage rows, so an operator can disable or retune
    any of them and this pass will leave that choice alone.
    """
    store = storage_provider.get_storage(ToolApprovalPolicy)
    created: list[str] = []
    for tool_name in CRUD_TOOL_NAMES:
        policy_id = crud_policy_id(tool_name)
        if await store.get(policy_id) is not None:
            continue
        try:
            await store.create(
                ToolApprovalPolicy(
                    id=policy_id,
                    toolset_id=CRUD_TOOLSET_ID,
                    tool_name=tool_name,
                    enabled=True,
                    approval=RequiredApprovalConfig(),
                )
            )
        except ConflictError:
            # Cross-process boot race: the desired end state holds.
            logger.debug("seed: approval policy %r created concurrently", policy_id)
            continue
        created.append(policy_id)
    if created:
        logger.info("seed: created %d default crud approval policies", len(created))
    return created


async def ensure_default_workspace(
    storage_provider, *, workspace_registry, template_id: str | None = None,
) -> str | None:
    """Ensure the default ``primer`` workspace exists.

    Materialises it from the reserved ``local-default`` template and writes
    the durable row exactly as ``POST /v1/workspaces`` does (phase="running"
    because materialise returned a live handle). The workspace's main
    session arrives via S1's workspace-creation seeding, not here.
    """
    from datetime import datetime, timezone

    from primer.bootstrap.defaults import (
        RESERVED_DEFAULT_WORKSPACE,
        RESERVED_LOCAL_WORKSPACE_TEMPLATE,
    )
    from primer.model.workspace import Workspace, WorkspaceTemplate

    workspaces = storage_provider.get_storage(Workspace)
    if await workspaces.get(RESERVED_DEFAULT_WORKSPACE) is not None:
        return None
    if workspace_registry is None:
        logger.info("seed: no workspace registry; default workspace deferred")
        return None
    # The deployment declares which template the default workspace comes
    # from (config: default_workspace_template). The reserved local
    # template stays the fallback for bare installs, but a k8s deployment
    # must name a durable provider's template or its default workspace
    # dies at the first pod replacement.
    wanted = template_id or RESERVED_LOCAL_WORKSPACE_TEMPLATE
    template = await storage_provider.get_storage(WorkspaceTemplate).get(wanted)
    if template is None:
        logger.info(
            "seed: workspace template %r absent; default workspace deferred",
            wanted,
        )
        return None
    live = await workspace_registry.materialise(
        template=template, workspace_id=RESERVED_DEFAULT_WORKSPACE,
    )
    try:
        await workspaces.create(
            Workspace(
                id=RESERVED_DEFAULT_WORKSPACE,
                name="primer",
                template_id=wanted,
                provider_id=template.provider_id,
                created_at=datetime.now(timezone.utc),
                # phase="running" because materialise returned a live
                # handle; runtime_meta is required and comes straight off
                # that handle, exactly as POST /v1/workspaces does.
                phase="running",
                runtime_meta=live.runtime_meta,
            )
        )
    except ConflictError:
        logger.debug("seed: default workspace row created concurrently")
        return None
    logger.info("seed: created default workspace %r", RESERVED_DEFAULT_WORKSPACE)
    return RESERVED_DEFAULT_WORKSPACE


async def default_profile_id(storage_provider) -> str | None:
    """Id of the profile seeded agents bind to, or None when none exists.

    On a wizard-provisioned install exactly one profile exists at seeding
    time, so the first row is unambiguous.
    """
    from primer.model.model_profile import ModelProfile
    from primer.model.storage import OffsetPage

    page = await storage_provider.get_storage(ModelProfile).list(
        OffsetPage(offset=0, length=1)
    )
    return page.items[0].id if page.items else None


async def ensure_seeded_agents(storage_provider) -> list[str]:
    """Ensure the operator and builder rows, then stamp default_agent_id.

    No-op while no ModelProfile exists (amendment C3): the wizard creates
    one, and the next pass repairs the agents. Existing rows are NEVER
    overwritten here; POST /v1/setup/reset_agents is the explicit,
    admin-only way back to these defaults.
    """
    from primer.bootstrap.defaults import (
        RESERVED_BUILDER_AGENT,
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
    from primer.model.agent import Agent

    profile_id = await default_profile_id(storage_provider)
    if profile_id is None:
        logger.info("seed: no model profile yet; agent seeding deferred")
        return []
    agents = storage_provider.get_storage(Agent)
    created: list[str] = []
    for agent_id, factory in (
        (RESERVED_OPERATOR_AGENT, operator_agent),
        (RESERVED_BUILDER_AGENT, builder_agent),
        (RESERVED_PLANNER_AGENT, planner_agent),
        (RESERVED_EXPLORER_AGENT, explorer_agent),
        (RESERVED_TOOL_RUNNER_AGENT, tool_runner_agent),
    ):
        if await agents.get(agent_id) is not None:
            continue
        try:
            await agents.create(factory(profile_id))
        except ConflictError:
            logger.debug("seed: agent %r created concurrently", agent_id)
            continue
        created.append(agent_id)
    state = await storage_provider.get_system_state()
    if state.default_agent_id != RESERVED_OPERATOR_AGENT:
        await storage_provider.set_default_agent_id(RESERVED_OPERATOR_AGENT)
    if created:
        logger.info("seed: created agents %r", created)
    return created


@dataclass
class EnsureResult:
    """Outcome of one :func:`run_ensure_pass` call.

    ``errors`` carries ``(step, repr(exception))`` pairs. Unlike
    BootstrapRunner there is no marker to withhold: the next boot simply
    runs the pass again.
    """

    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)


async def run_ensure_pass(
    storage_provider,
    *,
    workspace_registry=None,
    toolset_providers: dict | None = None,
    provider_registry=None,
    semantic_search_registry=None,
    default_workspace_template: str | None = None,
) -> EnsureResult:
    """Run every S5 ensure step. Idempotent, marker-independent.

    Called at every startup AND explicitly by the setup wizard's completion
    step, which is what closes amendment C3's ordering hole: the agents
    need a model profile that only exists after the wizard, so the wizard
    re-runs the pass the moment it has created one.

    Each step is independent: a failure is recorded and the others still
    run, mirroring :class:`primer.bootstrap.runner.BootstrapRunner`.
    """
    from primer.bootstrap.defaults import (
        RESERVED_BUILDER_AGENT,
        RESERVED_OPERATOR_AGENT,
    )
    from primer.knowledge.system_collection import regenerate_system_collection

    result = EnsureResult()

    async def _step(name: str, coro_factory):
        try:
            return await coro_factory()
        except Exception as exc:  # noqa: BLE001 - one bad step must not stop the rest
            logger.exception("seed: step %s failed: %s", name, exc)
            result.errors.append((name, repr(exc)))
            return None

    created_policies = await _step(
        "crud_approval_policies",
        lambda: ensure_crud_approval_policies(storage_provider),
    )
    result.created.extend(created_policies or [])

    created_agents = await _step(
        "seeded_agents", lambda: ensure_seeded_agents(storage_provider),
    )
    result.created.extend(created_agents or [])
    # `skipped` means DEFERRED, not "nothing to do": an already-seeded
    # install creates nothing and is not skipped. The only deferral is
    # amendment C3's, and its cause is the absent profile.
    if await default_profile_id(storage_provider) is None:
        result.skipped.extend(
            [RESERVED_OPERATOR_AGENT, RESERVED_BUILDER_AGENT]
        )

    created_workspace = await _step(
        "default_workspace",
        lambda: ensure_default_workspace(
            storage_provider, workspace_registry=workspace_registry,
            template_id=default_workspace_template,
        ),
    )
    if created_workspace:
        result.created.append(created_workspace)

    # The registries let regeneration re-index the documents it rewrites,
    # so a platform change shows up in search without a re-bootstrap. It
    # writes only what actually differs, so the cost tracks the diff.
    written = await _step(
        "system_collection",
        lambda: regenerate_system_collection(
            storage_provider,
            toolset_providers=toolset_providers or {},
            provider_registry=provider_registry,
            semantic_search_registry=semantic_search_registry,
        ),
    )
    if written:
        logger.info("seed: system collection wrote %d documents", written)
    return result


__all__ = [
    "crud_policy_id",
    "ensure_crud_approval_policies",
    "ensure_default_workspace",
    "ensure_seeded_agents",
    "default_profile_id",
    "EnsureResult",
    "run_ensure_pass",
]
