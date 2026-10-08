"""How a tool's argument schema is found from outside an agent turn.

The policy write checks (``primer/agent/approval_checks.py``) need the JSON Schema of the tool a policy gates, to say whether each of its ``preview_args`` names an
argument of that tool. Inside an agent turn the tool manager already holds every descriptor; at write time there is no turn, so the schema is looked up here:

* every toolset the provider registry holds (the reserved ones and the user-made ones) is asked to list its tools, under a clock: a toolset backed by an unreachable MCP
  server blocks rather than raising, and a policy write must not wait on it;
* the ``workspace`` toolset is no registry's: its tools exist per workspace session, so their argument models are taken from the table below.

A tool that cannot be found answers ``None``; the check then says the paths cannot be checked instead of guessing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from primer.workspace.local import tools as _local_tools

if TYPE_CHECKING:
    from primer.api.registries.provider_registry import ProviderRegistry

logger = logging.getLogger(__name__)

# The ceiling for listing one toolset's tools while a policy is being written (the Tools page uses the same figure per toolset).
LIST_TIMEOUT_S = 10.0

# The id the tool manager gives the workspace tools' toolset (``primer.agent.tool_manager.WORKSPACE_TOOLSET_ID``); imported there, so it is not imported here.
WORKSPACE_TOOLSET = "workspace"

# Workspace tool id -> its argument model. The local and the sandbox backends implement the same seven tools with the same arguments; tests/agent/
# test_approval_checks_preview_args.py pins this table against the local classes so a new tool or a renamed model fails there.
WORKSPACE_TOOL_ARGS: dict[str, type[BaseModel]] = {
    _local_tools.Edit.id: _local_tools.EditArgs,
    _local_tools.Exec.id: _local_tools.ExecArgs,
    _local_tools.Glob.id: _local_tools.GlobArgs,
    _local_tools.Grep.id: _local_tools.GrepArgs,
    _local_tools.Ls.id: _local_tools.LsArgs,
    _local_tools.Read.id: _local_tools.ReadArgs,
    _local_tools.Write.id: _local_tools.WriteArgs,
}


async def find_tool_schema(provider_registry: "ProviderRegistry", toolset_id: str, tool_name: str) -> dict[str, Any] | None:
    """The ``args_schema`` of tool ``tool_name`` of toolset ``toolset_id``, or ``None`` when it cannot be found (an unknown toolset or tool, a toolset that fails to
    list, or one that does not answer within :data:`LIST_TIMEOUT_S`). Never raises for those: the caller turns ``None`` into a refusal that says so."""
    if toolset_id == WORKSPACE_TOOLSET:
        model = WORKSPACE_TOOL_ARGS.get(tool_name)
        return model.model_json_schema() if model is not None else None
    try:
        provider = await provider_registry.get_toolset(toolset_id)
        async with asyncio.timeout(LIST_TIMEOUT_S):
            async for tool in provider.list_tools(principal=None):
                if tool.id == tool_name:
                    return tool.args_schema
    except Exception as exc:  # noqa: BLE001 - one broken toolset must not fail a policy write; the check reports "cannot be checked"
        logger.warning("tool schema lookup for %s::%s failed: %s: %s", toolset_id, tool_name, type(exc).__name__, exc)
    return None


__all__ = ["LIST_TIMEOUT_S", "WORKSPACE_TOOL_ARGS", "WORKSPACE_TOOLSET", "find_tool_schema"]
