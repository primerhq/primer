"""tool.called emission through a wired ToolExecutionManager.

Regression: the first wiring read ``result.is_error`` on the
ToolResultPart execute() returns (the field is ``error``), which
crashed every recorded tool call in the live loop while all unit
lanes ran recorder-less managers. This suite runs the manager WITH a
recorder so the emission path itself is under test.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest_asyncio

from primer.agent.tool_manager import ToolExecutionManager
from primer.events.recorder import EventRecorder
from primer.model.chat import Tool, ToolCallPart, ToolCallResult
from primer.model.principal import PrincipalRef
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider

# asyncio_mode = "auto" in pyproject.toml: async tests need no marker.


class _FakeToolsetProvider:
    def __init__(self, *, toolset_id: str, tools: list[Tool],
                 fail: bool = False) -> None:
        self._toolset_id = toolset_id
        self._tools = tools
        self._fail = fail

    def required_role(self, tool_name: str) -> str:
        return "user"

    async def list_tools(self, *, principal: str | None = None):
        for t in self._tools:
            yield t

    async def call(self, *, tool_name: str, arguments: dict[str, Any],
                   principal: str | None = None, ctx=None) -> ToolCallResult:
        return ToolCallResult(
            output="boom" if self._fail else "fine", is_error=self._fail,
        )


def _tool(name: str, *, toolset_id: str) -> Tool:
    return Tool(
        id=name,
        description=f"a test tool named {name}",
        toolset_id=toolset_id,
        args_schema={"type": "object", "properties": {},
                     "additionalProperties": False},
    )


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "t.sqlite")))
    await provider.initialize()
    await provider.get_event_store().ensure_schema()
    try:
        yield provider
    finally:
        await provider.aclose()


async def test_execute_lands_tool_called_with_outcome(sp):
    fake = _FakeToolsetProvider(
        toolset_id="a", tools=[_tool("foo", toolset_id="a")],
    )
    mgr = ToolExecutionManager(
        toolset_providers={"a": fake},
        initiated_by=PrincipalRef.system(),
        event_recorder=EventRecorder(sp.get_event_store()),
    )
    result = await mgr.execute(
        ToolCallPart(id="c1", name="a__foo", arguments={}),
    )
    assert result.error is False

    [event] = await sp.get_event_store().read_after(0)
    assert event.event_type == "tool.called"
    assert event.payload == {"tool": "a__foo", "ok": True}


async def test_failed_call_records_ok_false(sp):
    fake = _FakeToolsetProvider(
        toolset_id="a", tools=[_tool("foo", toolset_id="a")], fail=True,
    )
    mgr = ToolExecutionManager(
        toolset_providers={"a": fake},
        initiated_by=PrincipalRef.system(),
        event_recorder=EventRecorder(sp.get_event_store()),
    )
    result = await mgr.execute(
        ToolCallPart(id="c1", name="a__foo", arguments={}),
    )
    assert result.error is True
    [event] = await sp.get_event_store().read_after(0)
    assert event.payload == {"tool": "a__foo", "ok": False}


async def test_recorderless_manager_emits_nothing(sp):
    fake = _FakeToolsetProvider(
        toolset_id="a", tools=[_tool("foo", toolset_id="a")],
    )
    mgr = ToolExecutionManager(
        toolset_providers={"a": fake},
        initiated_by=PrincipalRef.system(),
    )
    await mgr.execute(ToolCallPart(id="c1", name="a__foo", arguments={}))
    assert await sp.get_event_store().read_after(0) == []
