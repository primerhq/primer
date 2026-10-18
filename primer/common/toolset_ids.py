"""The retired underscore-prefixed toolset ids, and the one place that maps them to the current ones.

The four built-in toolsets that shipped as ``_system``, ``_workspaces``, ``_misc`` and ``_search`` lost the prefix. Agent rows, graph nodes and tool calls written before the
rename still say ``_system``, so the registry resolves the old id to the new provider (``ProviderRegistry.get_toolset``): the SAME toolset runs under either spelling.

Anything keyed on a toolset id has to judge the spellings as one. The approval gate is the case that matters: a policy on ``system::create_agent`` that a call spelled
``_system`` slips past is a bypass the operator cannot see (the call runs the very same tool), and so is a policy written for ``_system`` that a call spelled ``system``
slips past. ``ApprovalResolver.find`` and the policy uniqueness check read the spellings from here.

No imports, on purpose: ``primer.agent`` (the gate) needs this and cannot import ``primer.api.registries.provider_registry`` for it, because importing anything under
``primer.api`` builds the app and the app imports ``primer.agent``. ``provider_registry`` re-exports :func:`canonical_toolset_id`.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

# Backward-compat: the ids that shipped before the underscore-prefix convention was retired, each with the id the registry resolves it to. Remove an entry after a
# deprecation window (when the operator-saved configs have been re-saved).
TOOLSET_ID_ALIASES: Mapping[str, str] = MappingProxyType({
    "_system": "system",
    "_workspaces": "workspaces",
    "_search": "search",
    "_misc": "misc",
})


def canonical_toolset_id(toolset_id: str) -> str:
    """The id the registry resolves ``toolset_id`` to: a retired underscore-prefixed id maps to its current one, any other id is returned unchanged.

    Public so the agent reference check resolves a tool id's toolset exactly as a turn does, and the approval gate looks a call's policy up under the id the call RUNS
    under, not the one it was spelled with.
    """
    return TOOLSET_ID_ALIASES.get(toolset_id, toolset_id)


def toolset_id_spellings(toolset_id: str) -> tuple[str, ...]:
    """Every id that names the same toolset as ``toolset_id``: the canonical id first, then each retired alias of it.

    A toolset with no alias has one spelling (itself). A row stored under an alias before the rename, or written under one since (``ToolApprovalPolicy.toolset_id`` accepts any
    string), is found by asking for each of these.
    """
    canonical = canonical_toolset_id(toolset_id)
    return (canonical, *(alias for alias, current in TOOLSET_ID_ALIASES.items() if current == canonical))


__all__ = ["TOOLSET_ID_ALIASES", "canonical_toolset_id", "toolset_id_spellings"]
