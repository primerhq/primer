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
from typing import TYPE_CHECKING

import logging

from primer.model.except_ import ConflictError
from primer.model.tool_approval import (
    RequiredApprovalConfig,
    ToolApprovalPolicy,
)
from primer.toolset.crud import CRUD_TOOL_NAMES, CRUD_TOOLSET_ID

if TYPE_CHECKING:
    from primer.int.toolset import ToolsetProvider


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
    """Ensure the default workspace (prime) exists and is recorded in system state.

    Which id it has, in order (the module docstring of :mod:`primer.bootstrap.prime` has the full rule):

    1. the id already recorded in ``SystemState.default_workspace_id``;
    2. else an existing install's ``primer`` row, which is ADOPTED (recorded and named ``prime`` for display, never
       duplicated). This is what a database whose migrations were baselined (``auto_bootstrap: false``) needs, because
       migration 10, which does the same for an upgraded database, never ran there;
    3. else ``prime``, the fresh-install id.

    If the row for that id exists, nothing is created: the id is recorded when it was not (a workspace a person already
    called ``prime`` is adopted, not duplicated) and ``None`` is returned. Otherwise the workspace is materialised from the
    deployment's template (``template_id``, else the reserved ``local-default``) and the durable row is written exactly as
    ``POST /v1/workspaces`` does (phase="running" because materialise returned a live handle).

    **The id is recorded BEFORE the backend is materialised and the row written.** The row is written last, so a record made
    after it would leave an instant in which the row exists and :func:`~primer.bootstrap.prime.resolve_default_workspace_id`
    still answers ``None``: ``WorkspaceRegistry.destroy("prime")`` would not be refused then (the guard names the default
    through that resolver) and a delete landing in the gap could remove the default workspace. A pass that records and then
    fails (the backend is down) leaves a recorded id with no row, and the next pass creates the row under the recorded id.
    A pass that cannot create anything (no registry, template absent) records nothing: it never names a default it could not
    make.

    Returns the id only when THIS call created the row (a concurrent creator's ``ConflictError`` is not an error and returns
    ``None``). The workspace's main operator session is not seeded here: it is per user, made on request by
    ``POST /v1/workspaces/{id}/main_session`` (see :func:`ensure_main_operator_session`).
    """
    from datetime import datetime, timezone

    from primer.bootstrap.defaults import (
        PRIME_WORKSPACE_ID,
        PRIME_WORKSPACE_NAME,
        RESERVED_LOCAL_WORKSPACE_TEMPLATE,
    )
    from primer.bootstrap.prime import adopt_legacy_default_workspace, record_default_workspace_id
    from primer.model.workspace import Workspace, WorkspaceTemplate

    workspaces = storage_provider.get_storage(Workspace)
    state = await storage_provider.get_system_state()
    workspace_id = (
        getattr(state, "default_workspace_id", None)
        or await adopt_legacy_default_workspace(storage_provider)
        or PRIME_WORKSPACE_ID
    )
    if await workspaces.get(workspace_id) is not None:
        await record_default_workspace_id(storage_provider, workspace_id)
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
    await record_default_workspace_id(storage_provider, workspace_id)
    live = await workspace_registry.materialise(
        template=template, workspace_id=workspace_id,
    )
    try:
        await workspaces.create(
            Workspace(
                id=workspace_id,
                name=PRIME_WORKSPACE_NAME,
                template_id=wanted,
                template_snapshot=template,
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
    logger.info("seed: created default workspace %r", workspace_id)
    return workspace_id


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


SYSTEM_CDC_SUBSCRIPTION = "system-cdc"
SYSTEM_LOGGER_SUBSCRIPTION = "system-logger"
SYSTEM_FLIP_SUBSCRIPTION = "system-session-flip"


async def ensure_system_event_subscriptions(storage_provider) -> list[str]:
    """Seed (and repair) the two system event subscriptions.

    * ``system-cdc``: converges the system collection on every agent /
      graph / collection event - exactly the kinds ``converge_entity``
      handles, so page writes (document events) never re-enter the sink.
    * ``system-logger``: one structured log line per platform event,
      minus the high-volume ``tool.called``.

    Same contract as the seeded agents: absent rows are recreated,
    present rows are left alone (operators may pause them).
    """
    from primer.model.event import (
        ConvergeSink,
        EventFilter,
        EventSubscription,
        FlipSink,
        LogSink,
    )

    subs = storage_provider.get_storage(EventSubscription)
    wanted = [
        EventSubscription(
            id=SYSTEM_CDC_SUBSCRIPTION,
            description=(
                "Converges the system collection page of every mutated "
                "agent, graph and collection (the CDC path)."
            ),
            filter=EventFilter(
                event_types=["agent.*", "graph.*", "collection.*"],
            ),
            sink=ConvergeSink(),
            managed_by="system",
        ),
        EventSubscription(
            id=SYSTEM_LOGGER_SUBSCRIPTION,
            description=(
                "Default system logger: one structured log line per "
                "platform event (tool.called excluded for volume)."
            ),
            filter=EventFilter(
                event_types=["*"],
                exclude_types=[
                    "tool.called", "llm.called", "workspace.file_changed",
                ],
            ),
            sink=LogSink(),
            managed_by="system",
        ),
        EventSubscription(
            id=SYSTEM_FLIP_SUBSCRIPTION,
            description=(
                "Replays durable session.wake events into parked-session "
                "flips (the volatile bus keeps the fast path; this closes "
                "the lost-publish window)."
            ),
            filter=EventFilter(event_types=["session.wake"]),
            sink=FlipSink(),
            managed_by="system",
        ),
    ]
    created: list[str] = []
    for row in wanted:
        existing = await subs.get(row.id)
        if existing is not None:
            # Converge system-owned config (filter / sink / description)
            # so a shipped default change reaches existing installs;
            # the operator's paused switch is theirs and is preserved.
            if (
                existing.filter != row.filter
                or existing.sink != row.sink
                or existing.description != row.description
            ):
                await subs.update(row.model_copy(
                    update={"paused": existing.paused},
                ))
            continue
        try:
            await subs.create(row)
            created.append(row.id)
        except ConflictError:
            pass  # concurrent seeder won the race; converged either way
    return created


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

    created_subs = await _step(
        "system_event_subscriptions",
        lambda: ensure_system_event_subscriptions(storage_provider),
    )
    result.created.extend(created_subs or [])

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


async def builtin_toolset_providers(provider_registry) -> dict[str, ToolsetProvider]:
    """The toolset providers of every built-in toolset, from the live registry.

    The S5 world is regenerated in two places - the boot ensure pass and the
    setup seed route - and both must cover every built-in toolset, so the map
    is built here, once, instead of each caller carrying its own copy. A
    toolset the registry cannot build raises a PrimerError; it is logged with
    its id and skipped so one missing toolset does not drop the rest.
    """
    from primer.api.registries.provider_registry import RESERVED_TOOLSET_IDS
    from primer.model.except_ import PrimerError

    providers: dict[str, ToolsetProvider] = {}
    for tid in sorted(RESERVED_TOOLSET_IDS):
        try:
            providers[tid] = await provider_registry.get_toolset(tid)
        except PrimerError as err:
            logger.warning("seed: skipping built-in toolset %r: %s", tid, err)
    return providers


__all__ = [
    "builtin_toolset_providers",
    "crud_policy_id",
    "ensure_crud_approval_policies",
    "ensure_default_workspace",
    "ensure_seeded_agents",
    "ensure_system_event_subscriptions",
    "default_profile_id",
    "EnsureResult",
    "run_ensure_pass",
]
