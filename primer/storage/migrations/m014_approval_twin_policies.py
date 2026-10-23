"""Migration 14: gate every door to the crud writes on an upgraded install (audit G6-1).

The seeded approval policies gated the ``crud`` tools only. The same writes have twins that were never gated: the system toolset's
``create_agent`` / ``update_agent`` / ``delete_agent`` and the graph trio, and the trigger toolset's ``create`` / ``update`` / ``delete``.
They are role user and ``system__call_tool``, which every seeded agent holds, reaches them, so a model could rewrite any agent's prompt with no
approval card. The ensure pass now seeds a policy for each twin (``primer.toolset.crud.CRUD_TWINS``); this migration gives an install that
predates it the same gates, and gives each twin the gate its crud sibling has NOW:

* a twin that already has a policy (under any id or spelling) is left alone: it is the operator's, and a second row would be a duplicate;
* otherwise, when the crud sibling's policy is a plain ``required`` gate, the twin's policy copies its ``enabled``, ``timeout_seconds``,
  ``approvers`` and ``preview_args`` (the sibling's arguments are the twin's): an operator who relaxed or retuned the gate on creating agents keeps
  that choice on every door to it, and an untouched install gets the shipped ``required`` gate, as a fresh install does. The sibling is the seeded
  row when it exists, else the lowest-id row for the sibling's key;
* a CONDITIONAL sibling gate (a Rego policy or an LLM judge) is not copied: its condition was written for the crud call and may read
  ``input.tool_name`` or ``input.toolset_id``, which differ on the twin, so a copy could never fire there. The twin gets the shipped gate and a
  WARNING names both policy ids, so the operator can retune the twin;
* a delete has no crud sibling, and a sibling with no row at all (deleted; the ensure pass re-creates it at the shipped default) gives the
  shipped gate: enabled, ``required``.

What the migration reads and writes is frozen here, as it was when it shipped: the twin table, the policy ids (``tool-approval-policy-<toolset>-
<tool>``, the ids the ensure pass uses, so the pass that runs after this finds them and seeds nothing) and the retired spelling of ``system``. A
later change to the live table must not change what this step did. Create-only: a concurrent writer's row wins (``ConflictError`` is skipped).
The view redefines ``ToolApprovalPolicy`` so a stored row the live model may reject is still read and copied as stored (see m002/m003 for the
idiom).
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

# ``(toolset_id, tool_name, crud sibling or None)``: the twins of the crud tools as this step gates them (frozen; the live table is
# ``primer.toolset.crud.CRUD_TWINS``).
_TWINS: tuple[tuple[str, str, str | None], ...] = (
    ("system", "create_agent", "create_agent"),
    ("system", "update_agent", "update_agent"),
    ("system", "delete_agent", None),
    ("system", "create_graph", "create_graph"),
    ("system", "update_graph", "update_graph"),
    ("system", "delete_graph", None),
    ("trigger", "create", "create_trigger"),
    ("trigger", "update", "update_trigger"),
    ("trigger", "delete", None),
)

# Every id a policy for these toolsets may be stored under: ``_system`` is the retired spelling of ``system`` (frozen; the live map is
# ``primer.common.toolset_ids``).
_SPELLINGS: dict[str, tuple[str, ...]] = {"system": ("system", "_system"), "trigger": ("trigger",), "crud": ("crud",)}

# The fields of a plain ``required`` policy that say what its gate does; everything else (its id and key) is the twin's own.
_COPIED_FIELDS = ("enabled", "approval", "timeout_seconds", "approvers", "preview_args")

# The shipped gate, as the ensure pass writes it.
_SHIPPED_GATE: dict[str, Any] = {"enabled": True, "approval": {"type": "required"}}


class ToolApprovalPolicy(Identifiable):  # noqa: N801 - the class name selects the table
    """Migration-local view: the key, and every other stored field kept as it is."""

    model_config = ConfigDict(extra="allow")

    toolset_id: str
    tool_name: str


def _policy_id(toolset_id: str, tool_name: str) -> str:
    return f"tool-approval-policy-{toolset_id}-{tool_name.replace('_', '-')}"


async def _rows_for(store, toolset_id: str, tool_name: str) -> list[ToolApprovalPolicy]:
    """Every stored policy of ``(toolset_id, tool_name)``, enabled or not, under every spelling of the toolset."""
    rows: dict[str, ToolApprovalPolicy] = {}
    for spelling in _SPELLINGS[toolset_id]:
        predicate = Q(ToolApprovalPolicy).where("toolset_id", spelling).where("tool_name", tool_name).build()
        page = await store.find(predicate, OffsetPage(offset=0, length=50))
        rows.update((row.id, row) for row in page.items)
    return list(rows.values())


async def _sibling_gate(store, twin_id: str, sibling: str) -> dict[str, Any]:
    """The gate the twin of crud tool ``sibling`` gets: a copy of the sibling's plain ``required`` policy, else the shipped gate."""
    rows = await _rows_for(store, "crud", sibling)
    if not rows:
        return dict(_SHIPPED_GATE)
    seeded = _policy_id("crud", sibling)
    row = next((r for r in rows if r.id == seeded), None) or min(rows, key=lambda r: r.id)
    stored = row.model_dump(mode="json")
    if (stored.get("approval") or {}).get("type") != "required":
        logger.warning(
            "approval policy %r on crud tool %r is conditional (%s), so its twin %r gets the shipped required gate instead of a copy: "
            "the condition was written for the crud call. Retune %r if the twin should be gated the same way.",
            row.id, sibling, (stored.get("approval") or {}).get("type"), twin_id, twin_id,
        )
        return dict(_SHIPPED_GATE)
    return {name: stored[name] for name in _COPIED_FIELDS if name in stored}


class M014ApprovalTwinPolicies:
    """Seed the approval policies of the crud tools' twins, each gated as its crud sibling is gated."""

    version = 14
    description = "Gate the twins of the crud tools (system and trigger writes) as their crud siblings are gated"

    async def apply(self, sp: StorageProvider) -> None:
        store = sp.get_storage(ToolApprovalPolicy)
        created: list[str] = []
        for toolset_id, tool_name, sibling in _TWINS:
            policy_id = _policy_id(toolset_id, tool_name)
            if await store.get(policy_id) is not None or await _rows_for(store, toolset_id, tool_name):
                continue
            gate = await _sibling_gate(store, policy_id, sibling) if sibling is not None else dict(_SHIPPED_GATE)
            try:
                await store.create(ToolApprovalPolicy(id=policy_id, toolset_id=toolset_id, tool_name=tool_name, **gate))
            except ConflictError:
                continue
            created.append(policy_id)
        if created:
            logger.info("gated the twins of the crud tools", extra={"policies": created})


__all__ = ["M014ApprovalTwinPolicies"]
