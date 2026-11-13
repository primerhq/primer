"""Migration 33: give the internal-collections writes the shipped admin-tier gate (the Tools Universal M4 recut of the wiki-first M2 ruling).

The three internal-collections writes (``put_config``, ``delete_config``, ``bootstrap``) are generated route tools, so the generated-mutator half of the
seed (``primer.api.route_tools._publish_generated_default_gates``) registered them in ``crud.DEFAULT_GATED_TOOLS`` from the first app build: an install
that booted after that build has a policy row per write, seeded by ``primer.bootstrap.seed.ensure_crud_approval_policies`` as a required gate with NO
approver spec, because the writes were not yet in the admin-tier table (``primer.toolset.admin_gates.ADMIN_GATES``). M4 moves them into that table, so
the shipped gate is a required gate decided by an admin only; this step brings an upgraded install to the same rows:

* a tool with no policy row at all gets the shipped default (the id ``tool-approval-policy-internal-collections-<tool>``, enabled, required, the
  admin-only approver spec);
* a row under the seeded id still exactly as the pre-admin-tier seed wrote it (enabled, a required gate, no timeout, no approvers, no preview paths) is
  re-stamped to the shipped default;
* a row the operator changed (disabled it, retuned it, named its approvers or preview paths, made it a policy gate), and a policy the operator wrote
  under another id, is the operator's gate and stays: no second row is written beside it.

Create-or-re-stamp only on the seeded id. What the step reads and writes is frozen here, as it was when it shipped: the table, the seeded shape, the
policy ids (``primer.bootstrap.seed.default_policy_id``) and the admin-only approver spec. A concurrent writer's row wins (``ConflictError`` is
skipped) and a row gone by the time of the update is skipped (``NotFoundError``), so a second run, or a run that died half way, changes nothing more.
The view redefines ``ToolApprovalPolicy`` so a stored row the live model may reject is still read (see m002/m003).
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ConfigDict

from primer.int.storage_provider import StorageProvider
from primer.model.common import Identifiable
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.storage import OffsetPage
from primer.storage.q import Q

logger = logging.getLogger(__name__)

_TOOLSET = "internal_collections"
_TOOLS: tuple[str, ...] = ("put_config", "delete_config", "bootstrap")


def _seeded(tool_name: str) -> dict[str, Any]:
    """The row as the generated-mutator seed wrote it before the writes joined the admin-tier table (``enabled`` defaulted to true, every optional
    field to null)."""
    return {
        "toolset_id": _TOOLSET,
        "tool_name": tool_name,
        "enabled": True,
        "approval": {"type": "required"},
        "timeout_seconds": None,
        "approvers": None,
        "preview_args": None,
    }


# Who may decide the shipped gate: an admin only (``primer.session.approvers.ADMIN_ONLY_SPEC`` as it shipped: no role and no user is named, and an
# admin is always admitted).
_ADMIN_ONLY: dict[str, Any] = {"kind": "roles", "roles": [], "users": []}


class ToolApprovalPolicy(Identifiable):  # noqa: N801 - the class name selects the table
    """Migration-local view: the key, and every other stored field kept as it is."""

    model_config = ConfigDict(extra="allow")

    toolset_id: str
    tool_name: str


def _policy_id(tool_name: str) -> str:
    return f"tool-approval-policy-{_TOOLSET}-{tool_name.replace('_', '-')}"


def _as_seeded(row: ToolApprovalPolicy) -> bool:
    """Whether the stored row is the pre-admin-tier seeded gate, unchanged. A field the row does not carry reads as its default (an older row may omit one)."""
    stored = row.model_dump()
    defaults = {"enabled": True, "timeout_seconds": None, "approvers": None, "preview_args": None}
    seeded = _seeded(row.tool_name)
    return all(stored.get(key, defaults.get(key)) == value for key, value in seeded.items())


async def _has_policy(store, tool_name: str) -> bool:
    """Whether any policy of the tool is stored, enabled or not, under any id."""
    predicate = Q(ToolApprovalPolicy).where("toolset_id", _TOOLSET).where("tool_name", tool_name).build()
    return bool((await store.find(predicate, OffsetPage(offset=0, length=1))).items)


class M033InternalCollectionsAdminGates:
    """Create the missing gates and re-stamp the unchanged seeded ones of the internal-collections writes to the shipped admin-tier shape."""

    version = 33
    description = (
        "Gate the internal-collections writes (put_config, delete_config, bootstrap) as admin-tier writes with admin-only approvers"
    )

    async def apply(self, sp: StorageProvider) -> None:
        store = sp.get_storage(ToolApprovalPolicy)
        created: list[str] = []
        restamped: list[str] = []
        for tool_name in _TOOLS:
            policy_id = _policy_id(tool_name)
            row = await store.get(policy_id)
            if row is None:
                if await _has_policy(store, tool_name):
                    # The operator's row, under another id: a second row would be a duplicate, which the gate reads as
                    # unconditional and decided by an admin only (``primer.agent.approval._choose_policy``).
                    continue
                try:
                    await store.create(
                        ToolApprovalPolicy(
                            id=policy_id, toolset_id=_TOOLSET, tool_name=tool_name, enabled=True,
                            approval={"type": "required"}, approvers=dict(_ADMIN_ONLY),
                        ),
                    )
                except ConflictError:
                    # Cross-process boot race: the desired end state holds.
                    continue
                created.append(policy_id)
                continue
            if not _as_seeded(row):
                logger.info("left the operator's gate on internal_collections::%s as it is", tool_name, extra={"policy": policy_id})
                continue
            dump = row.model_dump()
            dump["id"] = policy_id
            dump["approvers"] = dict(_ADMIN_ONLY)
            try:
                await store.update(ToolApprovalPolicy(**dump))
            except NotFoundError:
                # Gone by the time of the update: a concurrent writer settled it.
                continue
            restamped.append(policy_id)
        if created:
            logger.info("created the internal-collections gates", extra={"policies": created})
        if restamped:
            logger.info("re-stamped the unchanged internal-collections gates to the admin-tier shape", extra={"policies": restamped})


__all__ = ["M033InternalCollectionsAdminGates"]
