"""Migration 20: rekey the harness lifecycle approval policies to the bare tool id.

The harness toolset registered its tools under scoped ids (``harness__install`` and the like), and the ensure pass and migration 16
seeded their approval gates under those names. The live code renamed the tools to the bare ids (``install``, ...): the tool manager now
scopes them to the same wire ids the model always saw (``harness__install``, ...), and every door the gate reads (the tool manager,
``system__call_tool``, the MCP dispatch) looks the policy up under the bare name. Left alone, an upgraded install would keep its gates
stored under names nothing looks up anymore, and the harness install, sync and uninstall would run ungated.

This step moves the key, not the gate: every policy row stored at (``harness``, ``harness__<name>``) is re-stored at (``harness``,
``<name>``) with every other field preserved. The row keeps its id (approval records reference it) and its settings (the operator may
have tuned or disabled it; the ensure pass that runs after this finds the row by key and seeds nothing beside it). A key that already
holds a policy is left alone, and so is its old-key twin: the operator's row wins, and a second row would be a duplicate the gate reads
as unconditional and decided by an admin only. A row left behind under the old name is inert: nothing dispatches there anymore.

Create-and-update-only: a concurrent writer wins (``ConflictError`` is skipped). The view redefines ``ToolApprovalPolicy`` so a stored
row the live model may reject is still read (see m002/m003).
"""

from __future__ import annotations

import logging

from pydantic import ConfigDict

from primer.int.storage_provider import StorageProvider
from primer.model.common import Identifiable
from primer.model.except_ import ConflictError
from primer.model.storage import OffsetPage
from primer.storage.q import Q

logger = logging.getLogger(__name__)

# ``(stored tool_name, live tool_name)``: the harness lifecycle gates as the seeded rows store them, re-keyed to the bare tool ids
# (frozen; the live keys are ``primer.toolset.admin_gates.HARNESS_LIFECYCLE``).
_TOOLSET_ID = "harness"
_REKEY: tuple[tuple[str, str], ...] = (
    ("harness__install", "install"),
    ("harness__sync", "sync"),
    ("harness__uninstall", "uninstall"),
)


class ToolApprovalPolicy(Identifiable):  # noqa: N801 - the class name selects the table
    """Migration-local view: the key, and every other stored field kept as it is."""

    model_config = ConfigDict(extra="allow")

    toolset_id: str
    tool_name: str


class M020HarnessPolicyRekey:
    """Rekey the harness install/sync/uninstall approval gates to the bare tool ids."""

    version = 20
    description = "Rekey the harness lifecycle approval gates from the scoped tool name to the bare one"

    async def apply(self, sp: StorageProvider) -> None:
        store = sp.get_storage(ToolApprovalPolicy)
        moved: list[str] = []
        for old_name, new_name in _REKEY:
            page = await store.find(
                Q(ToolApprovalPolicy).where("toolset_id", _TOOLSET_ID).where("tool_name", old_name).build(),
                OffsetPage(offset=0, length=50),
            )
            if not page.items:
                continue
            already = await store.find(
                Q(ToolApprovalPolicy).where("toolset_id", _TOOLSET_ID).where("tool_name", new_name).build(),
                OffsetPage(offset=0, length=1),
            )
            if already.items:
                continue
            for row in page.items:
                try:
                    await store.update(row.model_copy(update={"tool_name": new_name}))
                except ConflictError:
                    continue
                moved.append(row.id)
        if moved:
            logger.info("rekeyed the harness lifecycle gates to the bare tool id", extra={"policies": moved})


__all__ = ["M020HarnessPolicyRekey"]
