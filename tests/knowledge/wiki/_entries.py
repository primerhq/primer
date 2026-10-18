"""Tool entries for the wiki build tests.

``primer.toolset.tool_catalog.ToolEntry`` (pivot P1 task 5) is not on main when this
slice lands, so the tests build entries with the same fields and the same two derived
properties. The build reads an entry only through ``ToolEntryView``, which the real
entry satisfies, so nothing here changes when the catalog arrives.

``real_entries()`` is the production shape: every tool of the built-in toolsets that
``tests/docs/_live_toolsets.py`` builds, with the facts a catalog would derive (the role
fails closed to admin when undeclared, as ``InternalToolsetProvider.required_role`` does).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from primer.model.chat import Tool, ToolExample
from primer.toolset._describe import make_tool

DIRECT_CALL_TOOLSETS = frozenset({"workspace", "client"})

OBJECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"tz": {"type": "string"}},
    "additionalProperties": False,
}


@dataclass(frozen=True)
class StandInEntry:
    toolset_id: str
    tool: Tool
    required_role: str
    pauses_turn: bool
    needs_workspace: bool
    default_gate: str | None

    @property
    def scoped_id(self) -> str:
        return f"{self.toolset_id}__{self.tool.id}"

    @property
    def via_call_tool(self) -> bool:
        return self.toolset_id not in DIRECT_CALL_TOOLSETS


def make_entry(
    toolset_id: str,
    tool_id: str,
    *,
    purpose: str = "Do the thing.",
    when: str = "Use when you need the thing.",
    schema: dict[str, Any] | None = None,
    examples: list[ToolExample] | None = None,
    role: str = "user",
    pauses: bool = False,
    needs_workspace: bool = False,
    gate: str | None = None,
) -> StandInEntry:
    """An entry over a tool built the production way (``make_tool``: purpose, when, examples)."""
    tool = make_tool(
        id=tool_id,
        toolset_id=toolset_id,
        purpose=purpose,
        when=when,
        args_schema=schema if schema is not None else OBJECT_SCHEMA,
        examples=examples if examples is not None else [ToolExample(args={})],
        yields=pauses,
        requires_workspace=needs_workspace,
        required_role=role,
    )
    return StandInEntry(toolset_id, tool, role, pauses, needs_workspace, gate)


def entry_from_tool(tool: Tool) -> StandInEntry:
    """What a catalog derives for one built-in tool."""
    return StandInEntry(
        toolset_id=tool.toolset_id,
        tool=tool,
        required_role=tool.required_role or "admin",
        pauses_turn=tool.yields,
        needs_workspace=tool.requires_workspace,
        default_gate="required" if tool.toolset_id == "crud" else None,
    )


def _live_tools() -> dict[tuple[str, str], Tool]:
    from tests.docs._live_toolsets import built_in_tools

    seen: dict[tuple[str, str], Tool] = {}
    for toolset_id, spellings in built_in_tools().items():
        for tool in spellings.values():
            seen[(toolset_id, tool.id)] = tool
    return seen


def real_tool(toolset_id: str, tool_id: str) -> Tool:
    return _live_tools()[(toolset_id, tool_id)]


def real_entries() -> list[StandInEntry]:
    return [entry_from_tool(tool) for _, tool in sorted(_live_tools().items())]
