"""``call_tool`` is a pass-through: a Stop decides what it may cancel by the tool it WRAPS (task 01a10e2f, decision A4).

The loop used to ask ``tool_manager.is_interruptible(call.name)`` for the OUTER tool only, so ``system__call_tool`` (declared
interruptible, being a pass-through) was cancelled even when it wrapped a tool declared ``interruptible=False``
(``call_tool(system, put_document)`` was cancelled mid-write). Every declaration was bypassable through it.

Now ``ToolExecutionManager.is_interruptible_call(call)`` looks through ``call_tool`` to the wrapped (toolset_id, tool_name),
and the loop asks it for every call.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

# Importing primer.workspace makes ToolCallContext.model_rebuild() run (the forward reference to AgentSession).
import primer.workspace  # noqa: F401
from primer.agent.tool_manager import ToolExecutionManager
from primer.model.chat import Tool, ToolCallPart, ToolCallResult
from primer.model.principal import PrincipalRef
from tests.agent.test_loop_interrupt import _AFTER, _drive, _Manager, _ScriptedLLM, _tool_results, _tool_round

_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": True}
USER = PrincipalRef(type="user", id="u1", display="u1", role="user", source="local")


class _Provider:
    def __init__(self, toolset_id: str, tools: list[tuple[str, bool]]) -> None:
        self._toolset_id = toolset_id
        self._tools = tools

    def required_role(self, tool_name: str) -> str:
        return "user"

    async def list_tools(self, *, principal: str | None = None) -> AsyncIterator[Tool]:
        for tool_id, interruptible in self._tools:
            yield Tool(
                id=tool_id, description="d", toolset_id=self._toolset_id, args_schema=_SCHEMA, interruptible=interruptible,
            )

    async def call(self, *, tool_name: str, arguments: dict[str, Any], principal: str | None = None, ctx=None):
        return ToolCallResult(output="ok", is_error=False)


async def _manager() -> ToolExecutionManager:
    manager = ToolExecutionManager(
        toolset_providers={
            "system": _Provider("system", [("call_tool", True), ("create_agent", True)]),  # type: ignore[dict-item]
            "collections": _Provider("collections", [("create_document", False), ("read_document", True)]),  # type: ignore[dict-item]
        },
        initiated_by=USER,
    )
    await manager.list_tools()
    return manager


def _wrapping(toolset_id: Any, tool_name: Any, arguments: Any = None) -> ToolCallPart:
    return ToolCallPart(
        id="c1", name="system__call_tool",
        arguments={"toolset_id": toolset_id, "tool_name": tool_name, "arguments": arguments if arguments is not None else {}},
    )


async def test_call_tool_of_a_tool_that_must_not_be_cancelled_is_not_interruptible() -> None:
    manager = await _manager()

    assert manager.is_interruptible("system__call_tool") is True, "the outer tool, by name, is the pass-through's own flag"
    assert manager.is_interruptible_call(_wrapping("collections", "create_document")) is False


async def test_call_tool_of_an_interruptible_tool_is_interruptible() -> None:
    manager = await _manager()

    assert manager.is_interruptible_call(_wrapping("collections", "read_document")) is True
    assert manager.is_interruptible_call(_wrapping("system", "create_agent")) is True


async def test_call_tool_of_a_toolset_or_tool_the_manager_does_not_know_is_interruptible() -> None:
    """Unknown names are interruptible, as for ``is_interruptible`` (``execute`` refuses an unknown tool anyway), and a
    user-defined toolset's tools are not in the index of reserved declarations."""
    manager = await _manager()

    assert manager.is_interruptible_call(_wrapping("my_mcp_toolset", "anything")) is True
    assert manager.is_interruptible_call(_wrapping("collections", "no_such_tool")) is True


@pytest.mark.parametrize(
    "arguments",
    [{}, {"toolset_id": "collections"}, {"toolset_id": 1, "tool_name": "create_document"}, {"toolset_id": None, "tool_name": None}],
    ids=["no-args", "no-tool-name", "wrong-types", "nulls"],
)
async def test_malformed_call_tool_arguments_fall_back_to_the_outer_tools_own_flag(arguments: dict) -> None:
    manager = await _manager()

    assert manager.is_interruptible_call(ToolCallPart(id="c1", name="system__call_tool", arguments=arguments)) is True


async def test_call_tool_wrapping_call_tool_wrapping_a_protected_tool_is_still_protected() -> None:
    manager = await _manager()
    inner = {"toolset_id": "collections", "tool_name": "create_document", "arguments": {}}

    assert manager.is_interruptible_call(_wrapping("system", "call_tool", inner)) is False


async def test_call_tool_of_a_protected_tool_the_agent_allowlist_hides_is_still_protected() -> None:
    """``call_tool`` is how an agent reaches a tool that is NOT in its catalogue, so the protection cannot depend on the
    wrapped tool being visible to this agent."""
    manager = ToolExecutionManager(
        toolset_providers={
            "system": _Provider("system", [("call_tool", True)]),  # type: ignore[dict-item]
            "collections": _Provider("collections", [("create_document", False)]),  # type: ignore[dict-item]
        },
        initiated_by=USER,
        tools=["system__call_tool"],
    )
    visible = {t.id for t in await manager.list_tools()}
    assert visible == {"system__call_tool"}, "precondition: the wrapped tool is hidden from this agent"

    assert manager.is_interruptible_call(_wrapping("collections", "create_document")) is False


async def test_a_call_that_is_not_call_tool_is_decided_by_its_own_name() -> None:
    manager = await _manager()

    assert manager.is_interruptible_call(ToolCallPart(id="c1", name="collections__create_document", arguments={})) is False
    assert manager.is_interruptible_call(ToolCallPart(id="c1", name="collections__read_document", arguments={})) is True


class _LooksThroughManager(_Manager):
    """A manager whose OUTER tool ``loop_tool`` is interruptible by name, but whose per-call decision (the look-through)
    says this call wraps a tool that must not be cancelled."""

    def is_interruptible_call(self, call) -> bool:
        return False


async def test_the_loop_asks_the_per_call_decision_and_not_the_outer_tools_name() -> None:
    gate = asyncio.Event()
    manager = _LooksThroughManager(gate)                     # is_interruptible("loop_tool") is True by name
    interrupt = asyncio.Event()
    llm = _ScriptedLLM(_tool_round(1), _AFTER)

    async def stop_during_the_tool() -> None:
        await manager.started.wait()
        interrupt.set()
        await asyncio.sleep(0.05)
        gate.set()

    asyncio.get_running_loop().create_task(stop_during_the_tool())
    _, messages_out, interrupted = await _drive(llm, interrupt=interrupt, manager=manager, stop_after=None)

    assert manager.cancelled == 0, "a call the manager says wraps a protected tool was cancelled"
    assert manager.finished == 1 and interrupted == [True]
    assert [p.output for p in _tool_results(messages_out)] == ["ok"], "the real result, not a refusal"
