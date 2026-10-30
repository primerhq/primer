"""Migration 20: rekey the stored harness tool references to the bare tool ids.

The harness toolset registered its tools under scoped ids (``harness__install`` and the like), so their scoped wire
ids carried the prefix twice (``harness__harness__install``), and the ensure pass and migration 16 seeded the
approval gates under the scoped names. The live code renamed the tools to the bare ids (``install``, ...): every
stored reference now carries one ``harness__`` too many, at three places:

* approval policies (``ToolApprovalPolicy``): the gates are stored at (``harness``, ``harness__<name>``) while every
  door the gate reads (the tool manager, ``system__call_tool``, the MCP dispatch) looks the policy up under the bare
  name;
* the MCP exposure allowlist (``McpExposure.allowed_tools``, the singleton row): the scoped ids it holds are compared
  against the ids the endpoint exposes (``harness__<name>``);
* the agents' tool lists (``Agent.tools``): scoped ids of the same form.

This step moves the keys, not the data: it strips one ``harness__`` prefix from the harness references in each store
and preserves every other field. A policy row keeps its id (approval records reference it) and its settings (the
operator may have tuned or disabled it; the ensure pass that runs after this finds the row by key and seeds nothing
beside it). A policy key that already holds a row is left alone, and so is its old-key twin: the operator's row wins,
and a second row would be a duplicate the gate reads as unconditional and decided by an admin only. A row left behind
under the old key is inert: nothing dispatches there anymore.

Create-and-update-only: a concurrent writer wins (``ConflictError`` is skipped). Every store is read fully before any
write: an update can move a row within the list order, and paging while writing could skip one. The views redefine
the live models, so a stored row the live model may reject is still read (see m002/m003/m019).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from pydantic import ConfigDict, Field

from primer.int.storage_provider import StorageProvider
from primer.model.common import Identifiable
from primer.model.except_ import ConflictError
from primer.model.storage import OffsetPage
from primer.storage.q import Q

logger = logging.getLogger(__name__)

_PAGE = 200

_TOOLSET_ID = "harness"
#: The prefix the old bare ids and scoped ids carried. A stored reference is a harness one when it starts with this
#: (policies: the old bare name; the scoped ids: the doubled prefix).
_PREFIX = "harness__"


class ToolApprovalPolicy(Identifiable):  # noqa: N801 - the class name selects the table
    """Migration-local view of a policy row: the key, and every other stored field kept as it is."""

    model_config = ConfigDict(extra="allow")

    toolset_id: str
    tool_name: str


class McpExposure(Identifiable):  # noqa: N801 - the class name selects the table
    """Migration-local view of the exposure singleton: the allowlist, every other stored field kept as it is."""

    model_config = ConfigDict(extra="allow")

    allowed_tools: list[str] = Field(default_factory=list)


class Agent(Identifiable):  # noqa: N801 - the class name selects the table
    """Migration-local view of an agent row: the tool list, every other stored field kept as it is."""

    model_config = ConfigDict(extra="allow")

    tools: list[str] = Field(default_factory=list)


def _rekeyed(values: Sequence[str]) -> list[str]:
    """The scoped ids with one ``harness__`` prefix stripped; non-harness and already-bare ids pass through."""
    return [value.removeprefix(_PREFIX) if value.startswith(_PREFIX + _PREFIX) else value for value in values]


class M020HarnessPolicyRekey:
    """Rekey the stored harness tool references (approval gates, the MCP allowlist, agent tool lists) to the bare tool ids."""

    version = 20
    description = "rekey the stored harness tool references (approval gates, MCP allowlist, agent tools) to the bare tool id"

    async def apply(self, sp: StorageProvider) -> None:
        policies = sp.get_storage(ToolApprovalPolicy)
        moved_policies = await self._rekey_policies(policies)

        exposure = sp.get_storage(McpExposure)
        exposure_moved = await self._rekey_allowlist(exposure)

        agents = sp.get_storage(Agent)
        moved_agents = await self._rekey_agent_tools(agents)

        if moved_policies or exposure_moved or moved_agents:
            logger.info(
                "rekeyed the stored harness tool references to the bare tool id",
                extra={"policies": moved_policies, "mcp_exposure": exposure_moved, "agents": moved_agents},
            )

    async def _rekey_policies(self, store) -> list[str]:
        """Every harness policy row stored under a scoped tool name, re-stored under the bare one.

        Read fully before writing: the harness rows number in the single digits, and the move must see a consistent
        snapshot to decide which old keys already have a twin at the live key.
        """
        rows: list[ToolApprovalPolicy] = []
        offset = 0
        while True:
            page = await store.find(
                Q(ToolApprovalPolicy).where("toolset_id", _TOOLSET_ID).build(),
                OffsetPage(offset=offset, length=_PAGE),
            )
            rows.extend(page.items)
            if len(page.items) < _PAGE:
                break
            offset += _PAGE
        moved: list[str] = []
        for row in rows:
            if not row.tool_name.startswith(_PREFIX) or not row.tool_name.removeprefix(_PREFIX):
                continue  # not a scoped name, or stripping it would leave an empty name
            new_name = row.tool_name.removeprefix(_PREFIX)
            if any(other.id != row.id and other.tool_name == new_name for other in rows):
                continue  # the operator's row at the live key wins; the old-key twin is left inert
            try:
                await store.update(row.model_copy(update={"tool_name": new_name}))
            except ConflictError:
                continue
            moved.append(row.id)
        return moved

    async def _rekey_allowlist(self, store) -> bool:
        """The singleton's allowlist, the scoped ids with one ``harness__`` too many."""
        page = await store.list(OffsetPage(offset=0, length=_PAGE))
        row = next((item for item in page.items if item.id == "singleton"), None)
        if row is None or not any(entry.startswith(_PREFIX + _PREFIX) for entry in row.allowed_tools):
            return False
        try:
            await store.update(row.model_copy(update={"allowed_tools": _rekeyed(row.allowed_tools)}))
        except ConflictError:
            return False
        return True

    async def _rekey_agent_tools(self, store) -> list[str]:
        """Every agent row whose tool list holds a doubled scoped id, rewritten with one prefix stripped.

        Read fully before writing, the way m019 reads the channels: an update can move a row within the list order.
        """
        rows: list[Agent] = []
        offset = 0
        while True:
            page = await store.list(OffsetPage(offset=offset, length=_PAGE))
            rows.extend(page.items)
            if len(page.items) < _PAGE:
                break
            offset += _PAGE
        moved: list[str] = []
        for row in rows:
            rewritten = _rekeyed(row.tools)
            if rewritten == row.tools:
                continue
            try:
                await store.update(row.model_copy(update={"tools": rewritten}))
            except ConflictError:
                continue
            moved.append(row.id)
        return moved


__all__ = ["M020HarnessPolicyRekey"]
