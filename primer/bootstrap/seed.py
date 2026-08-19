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
    storage_provider, *, workspace_registry,
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
    template = await storage_provider.get_storage(WorkspaceTemplate).get(
        RESERVED_LOCAL_WORKSPACE_TEMPLATE
    )
    if template is None:
        logger.info(
            "seed: workspace template %r absent; default workspace deferred",
            RESERVED_LOCAL_WORKSPACE_TEMPLATE,
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
                template_id=RESERVED_LOCAL_WORKSPACE_TEMPLATE,
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


__all__ = [
    "crud_policy_id",
    "ensure_crud_approval_policies",
    "ensure_default_workspace",
]
