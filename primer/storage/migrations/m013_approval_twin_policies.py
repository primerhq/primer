"""Migration 13: gate every door to the crud writes on an upgraded install (audit G6-1).

The seeded approval policies gated the ``crud`` tools only. The same writes have twins that were never gated: the system toolset's
``create_agent`` / ``update_agent`` / ``delete_agent`` and the graph trio, and the trigger toolset's ``create`` / ``update`` / ``delete``
(``primer.toolset.crud.CRUD_TWINS``). They are role user and ``system__call_tool``, which every seeded agent holds, reaches them, so a model
could rewrite any agent's prompt with no approval card. The ensure pass now seeds a policy for each twin; this migration gives an install that
predates it the same gates, and gives each twin the gate its crud sibling has NOW:

* a twin that already has a policy (under any id or spelling) is left alone: it is the operator's, and a second row would be a duplicate;
* otherwise the twin's policy copies its crud sibling's ``enabled``, ``approval``, ``timeout_seconds``, ``approvers`` and ``preview_args`` (the
  sibling's arguments are the twin's): an operator who relaxed or retuned the gate on creating agents keeps that choice on every door to it, and
  an untouched install gets the shipped ``required`` gate, as a fresh install does. The sibling is the seeded row when it exists, else the
  lowest-id row for the sibling's key;
* a delete has no crud sibling, and a sibling with no row at all (deleted; the ensure pass re-creates it at the shipped default) gives the
  shipped default: enabled, ``required``.

The rows are written under the ids the ensure pass uses (``tool-approval-policy-<toolset>-<tool>``), so the pass that runs after this finds them
and seeds nothing. Create-only: a concurrent writer's row wins (``ConflictError`` is skipped). The view redefines ``ToolApprovalPolicy`` so a
stored row the live model may reject is still read and copied as stored (see m002/m003 for the idiom).
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ConfigDict

from primer.bootstrap.seed import crud_policy_id, default_policy_id, policy_rows_for
from primer.int.storage_provider import StorageProvider
from primer.model.common import Identifiable
from primer.model.except_ import ConflictError
from primer.toolset.crud import CRUD_TWINS

logger = logging.getLogger(__name__)

# The fields of a policy that say what its gate does; everything else (its id and key) is the twin's own.
_GATE_FIELDS = ("enabled", "approval", "timeout_seconds", "approvers", "preview_args")

# The shipped gate, as the ensure pass writes it.
_SHIPPED_GATE: dict[str, Any] = {"enabled": True, "approval": {"type": "required"}}


class ToolApprovalPolicy(Identifiable):  # noqa: N801 - the class name selects the table
    """Migration-local view: the key, and every other stored field kept as it is."""

    model_config = ConfigDict(extra="allow")

    toolset_id: str
    tool_name: str


async def _sibling_gate(store, sibling: str) -> dict[str, Any]:
    """The gate fields of crud tool ``sibling``'s policy, as stored; the shipped gate when it has none."""
    rows = await policy_rows_for(store, "crud", sibling)
    if not rows:
        return dict(_SHIPPED_GATE)
    seeded = crud_policy_id(sibling)
    row = next((r for r in rows if r.id == seeded), None) or min(rows, key=lambda r: r.id)
    stored = row.model_dump(mode="json")
    return {name: stored[name] for name in _GATE_FIELDS if name in stored}


class M013ApprovalTwinPolicies:
    """Seed the approval policies of the crud tools' twins, each copied from its crud sibling's."""

    version = 13
    description = "Gate the twins of the crud tools (system and trigger writes) as their crud siblings are gated"

    async def apply(self, sp: StorageProvider) -> None:
        store = sp.get_storage(ToolApprovalPolicy)
        created: list[str] = []
        for (toolset_id, tool_name), sibling in CRUD_TWINS.items():
            policy_id = default_policy_id(toolset_id, tool_name)
            if await store.get(policy_id) is not None or await policy_rows_for(store, toolset_id, tool_name, length=1):
                continue
            gate = await _sibling_gate(store, sibling) if sibling is not None else dict(_SHIPPED_GATE)
            try:
                await store.create(ToolApprovalPolicy(id=policy_id, toolset_id=toolset_id, tool_name=tool_name, **gate))
            except ConflictError:
                continue
            created.append(policy_id)
        if created:
            logger.info("gated the twins of the crud tools", extra={"policies": created})


__all__ = ["M013ApprovalTwinPolicies"]
