"""Migration 16: gate the admin-tier writes on an upgraded install (ticket 01a16a29, the admin half of audit G6-1).

Migration 14 gated every door to the agent, graph and trigger writes. A gate binds whatever the run's role, but three groups of writes had none, so a
run at an admin's rank (the seeded operator's main session usually runs at its creator's) walked around the approval system: the tool approval
policy writes (a model could delete the gate that protects it), the system toolset's ``create_toolset`` / ``update_toolset`` with a python or stdio
body (code on the server host; ``crud::create_python_toolset`` was gated) and the harness install, sync and uninstall. The ensure pass now seeds a
gate for each (``primer.toolset.admin_gates``); this migration gives an install that predates it the same rows:

* a tool that already has a policy (under any id, either spelling of ``system``, enabled or not) is left alone: it is the operator's, and a second
  row would be a duplicate, which the gate reads as unconditional and decided by an admin only;
* otherwise it gets the shipped gate: ``required`` (enabled) for the policy writes and the harness operations, and for the two toolset writes the
  Rego policy that parks a body that is not exactly an http or sse MCP toolset or an internal one; every one decided by an admin only
  (``approvers`` = the admin-only spec, the lead's ruling on #860).

There is no sibling to copy a gate from (migration 14 copied the crud sibling's): none of these writes has a crud twin with the same arguments.

What the migration reads and writes is frozen here, as it was when it shipped: the table, the Rego text, the policy ids
(``tool-approval-policy-<toolset>-<tool>``, the ids the ensure pass uses, so the pass that runs after this finds them and seeds nothing) and the
retired spelling of ``system``. A later change to the live table must not change what this step did. Create-only: a concurrent writer's row wins
(``ConflictError`` is skipped). The view redefines ``ToolApprovalPolicy`` so a stored row the live model may reject is still read (see m002/m003).
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ConfigDict

from primer.int.storage_provider import StorageProvider
from primer.model.common import Identifiable
from primer.model.except_ import ConflictError
from primer.model.storage import OffsetPage
from primer.storage.q import Q

logger = logging.getLogger(__name__)

# The toolset gate as it shipped (the live text is ``primer.toolset.admin_gates.HOST_CODE_TOOLSET_POLICY``).
_HOST_CODE_TOOLSET_POLICY = """package primer.tool_approval

# Shipped on system::create_toolset and system::update_toolset (ticket 01a16a29). A python toolset runs its source on the server
# host and a stdio MCP toolset launches a command there, so a write whose body is either pauses for approval. An allowlist: only a
# body that is exactly an http or sse MCP toolset, or an internal one, runs ungated. Any other body parks, including a provider or
# transport spelled another way or missing. To gate every toolset write, replace this policy with a required one.

default required := true

default reason := "a python toolset runs its source, and a stdio MCP toolset launches a command, on the server host"

required := false if {
    input.arguments.entity.provider == "mcp"
    input.arguments.entity.config.transport in {"http", "sse"}
}

required := false if {
    input.arguments.entity.provider == "internal"
}
"""

_REQUIRED: dict[str, Any] = {"type": "required"}
# Who may decide these gates: an admin only (``primer.session.approvers.ADMIN_ONLY_SPEC`` as it shipped: no role and no user is named, and an
# admin is always admitted).
_ADMIN_ONLY: dict[str, Any] = {"kind": "roles", "roles": [], "users": []}
_HOST_CODE: dict[str, Any] = {"type": "policy", "policy": _HOST_CODE_TOOLSET_POLICY}

# ``(toolset_id, tool_name, approval)``: the admin-tier writes as this step gates them (frozen; the live table is
# ``primer.toolset.admin_gates.ADMIN_GATES``).
_GATES: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("system", "create_tool_approval_policy", _REQUIRED),
    ("system", "update_tool_approval_policy", _REQUIRED),
    ("system", "delete_tool_approval_policy", _REQUIRED),
    ("system", "create_toolset", _HOST_CODE),
    ("system", "update_toolset", _HOST_CODE),
    ("harness", "harness__install", _REQUIRED),
    ("harness", "harness__sync", _REQUIRED),
    ("harness", "harness__uninstall", _REQUIRED),
)

# Every id a policy for these toolsets may be stored under: ``_system`` is the retired spelling of ``system`` (frozen; the live map is
# ``primer.common.toolset_ids``).
_SPELLINGS: dict[str, tuple[str, ...]] = {"system": ("system", "_system"), "harness": ("harness",)}


class ToolApprovalPolicy(Identifiable):  # noqa: N801 - the class name selects the table
    """Migration-local view: the key, and every other stored field kept as it is."""

    model_config = ConfigDict(extra="allow")

    toolset_id: str
    tool_name: str


def _policy_id(toolset_id: str, tool_name: str) -> str:
    return f"tool-approval-policy-{toolset_id}-{tool_name.replace('_', '-')}"


async def _has_policy(store, toolset_id: str, tool_name: str) -> bool:
    """Whether any policy of ``(toolset_id, tool_name)`` is stored, enabled or not, under any spelling of the toolset."""
    for spelling in _SPELLINGS[toolset_id]:
        predicate = Q(ToolApprovalPolicy).where("toolset_id", spelling).where("tool_name", tool_name).build()
        if (await store.find(predicate, OffsetPage(offset=0, length=1))).items:
            return True
    return False


class M016AdminRunGates:
    """Seed the shipped gates of the tool approval policy writes, the toolset writes that run code on the host, and the harness lifecycle."""

    version = 16
    description = "Gate the policy writes, the python and stdio toolset writes, and harness install/sync/uninstall, for admin-role runs too"

    async def apply(self, sp: StorageProvider) -> None:
        store = sp.get_storage(ToolApprovalPolicy)
        created: list[str] = []
        for toolset_id, tool_name, approval in _GATES:
            policy_id = _policy_id(toolset_id, tool_name)
            if await store.get(policy_id) is not None or await _has_policy(store, toolset_id, tool_name):
                continue
            try:
                await store.create(
                    ToolApprovalPolicy(
                        id=policy_id, toolset_id=toolset_id, tool_name=tool_name, enabled=True, approval=dict(approval),
                        approvers=dict(_ADMIN_ONLY),
                    ),
                )
            except ConflictError:
                continue
            created.append(policy_id)
        if created:
            logger.info("gated the admin-tier writes", extra={"policies": created})


__all__ = ["M016AdminRunGates"]
