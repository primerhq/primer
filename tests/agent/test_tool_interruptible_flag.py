"""The ``interruptible`` flag (stop slice B1): which tool calls a Stop may CANCEL.

A tool that is not interruptible (a file write: cancelling it releases the scope lock while its background thread still
writes, so a concurrent edit can lose an update) is waited for on a Stop instead of cancelled. The flag lives where each
kind of tool already declares its capabilities: a ``ClassVar`` on ``WorkspaceTool`` and a field on ``Tool`` (set through
``make_tool``), and the manager answers ``is_interruptible(scoped_name)`` from both.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from pydantic import BaseModel

from primer.agent.tool_manager import ToolExecutionManager
from primer.model.chat import Tool, ToolCallResult, tool_catalogue_flags
from primer.toolset._describe import make_tool
from primer.workspace.local.tools.edit import Edit
from primer.workspace.local.tools.exec_ import Exec
from primer.workspace.local.tools.glob import Glob
from primer.workspace.local.tools.grep import Grep
from primer.workspace.local.tools.ls import Ls
from primer.workspace.local.tools.read import Read
from primer.workspace.local.tools.write import Write
from primer.workspace.sandbox.tools.edit import SandboxEdit
from primer.workspace.sandbox.tools.exec_ import SandboxExec
from primer.workspace.sandbox.tools.glob import SandboxGlob
from primer.workspace.sandbox.tools.grep import SandboxGrep
from primer.workspace.sandbox.tools.ls import SandboxLs
from primer.workspace.sandbox.tools.read import SandboxRead
from primer.workspace.sandbox.tools.write import SandboxWrite
from primer.workspace.tool import WorkspaceTool

_SCHEMA = {"type": "object", "properties": {}}


def _tool(**overrides) -> Tool:
    base = {"id": "t", "toolset_id": "ts", "description": "d", "args_schema": _SCHEMA}
    base.update(overrides)
    return Tool(**base)


# --- the model ---------------------------------------------------------------------------------------------------------


def test_a_tool_is_interruptible_unless_it_says_otherwise() -> None:
    assert _tool().interruptible is True
    assert _tool(interruptible=False).interruptible is False


def test_make_tool_declares_it_explicitly() -> None:
    declared = make_tool(
        id="mutate", toolset_id="ts", purpose="Change two stores.", when="Use when both must change.",
        args_schema=_SCHEMA, examples=[], interruptible=False,
    )
    default = make_tool(
        id="read", toolset_id="ts", purpose="Read.", when="Use when reading.", args_schema=_SCHEMA, examples=[],
    )

    assert declared.interruptible is False
    assert default.interruptible is True


def test_the_flag_never_reaches_the_llm_facing_schema() -> None:
    """Like yields and requires_workspace it is in-memory metadata: excluded from the default dump."""
    assert "interruptible" not in _tool(interruptible=False).model_dump()


def test_the_picker_flags_carry_it_so_every_listing_route_and_the_mcp_table_show_it() -> None:
    """``tool_catalogue_flags`` is the one seam every "list tools" route (and the MCP exposure table) re-adds the
    excluded flags through, next to ``yields``."""
    assert tool_catalogue_flags(_tool())["interruptible"] is True
    assert tool_catalogue_flags(_tool(interruptible=False))["interruptible"] is False


# --- the workspace tools -----------------------------------------------------------------------------------------------

_FILE_MUTATORS = [Write, Edit, SandboxWrite, SandboxEdit]
_THE_REST = [Exec, Glob, Grep, Ls, Read, SandboxExec, SandboxGlob, SandboxGrep, SandboxLs, SandboxRead]


@pytest.mark.parametrize("tool_cls", _FILE_MUTATORS, ids=lambda c: c.__name__)
def test_the_file_mutators_are_not_interruptible(tool_cls) -> None:
    assert tool_cls.interruptible is False


@pytest.mark.parametrize("tool_cls", _THE_REST, ids=lambda c: c.__name__)
def test_every_other_workspace_tool_is_interruptible(tool_cls) -> None:
    """Exec above all: a Stop must be able to kill a command."""
    assert tool_cls.interruptible is True


def test_the_workspace_tool_base_defaults_to_interruptible() -> None:
    assert WorkspaceTool.interruptible is True


# --- the manager -------------------------------------------------------------------------------------------------------


class _Provider:
    """A toolset 'ts' with one interruptible and one not-interruptible tool."""

    async def list_tools(self, *, principal: str | None = None) -> AsyncIterator[Tool]:
        yield _tool(id="safe")
        yield _tool(id="mutator", interruptible=False)

    def is_yielding(self, tool_name: str) -> bool:
        return False

    def required_role(self, tool_name: str) -> str:
        return "admin"

    async def call(self, *, tool_name, arguments, principal=None, ctx=None) -> ToolCallResult:
        return ToolCallResult(output="ok", is_error=False)


class _Args(BaseModel):
    pass


class _FakeWorkspaceTool(WorkspaceTool):
    id = "fake"
    description = "d"

    def parameters(self):
        return _Args

    async def execute(self, args, ctx):
        raise NotImplementedError


class _FakeMutatingWorkspaceTool(_FakeWorkspaceTool):
    id = "mutates"
    interruptible = False


class _Session:
    session_id = "s"
    workspace_id = "w"


async def test_the_manager_answers_for_toolset_tools_by_scoped_name() -> None:
    manager = ToolExecutionManager(toolset_providers={"ts": _Provider()})
    await manager.list_tools()

    assert manager.is_interruptible("ts__safe") is True
    assert manager.is_interruptible("ts__mutator") is False


async def test_the_manager_answers_for_workspace_tools_by_scoped_name() -> None:
    manager = ToolExecutionManager(
        workspace_tools={"fake": _FakeWorkspaceTool(), "mutates": _FakeMutatingWorkspaceTool()},
        workspace_session=_Session(),  # type: ignore[arg-type]
    )
    await manager.list_tools()

    assert manager.is_interruptible("workspace__fake") is True
    assert manager.is_interruptible("workspace__mutates") is False


async def test_a_workspace_tools_catalogue_entry_carries_the_flag() -> None:
    manager = ToolExecutionManager(
        workspace_tools={"mutates": _FakeMutatingWorkspaceTool()},
        workspace_session=_Session(),  # type: ignore[arg-type]
    )
    (entry,) = await manager.list_tools()

    assert entry.interruptible is False
    assert tool_catalogue_flags(entry)["interruptible"] is False


async def test_an_unknown_tool_is_interruptible() -> None:
    """Cancelling is the default; an unknown name is refused by execute() anyway."""
    manager = ToolExecutionManager(toolset_providers={"ts": _Provider()})
    await manager.list_tools()

    assert manager.is_interruptible("ts__nope") is True
