"""ExternalToolsetProvider + ToolExecutionManager merge tests."""

from datetime import UTC, datetime

import pytest

from primer.agent.external_tools import (
    EXTERNAL_PARK_TOOL_NAME,
    EXTERNAL_TOOLSET_ID,
    ExternalToolsetProvider,
    external_event_key,
    external_resume_hook,
)
from primer.agent.tool_manager import ToolExecutionManager
from primer.model.chat import ToolCallPart
from primer.model.external_tool import ExternalToolDef
from primer.model.principal import PrincipalRef
from primer.model.yield_ import YieldCancelled, YieldTimeout, YieldToWorker
from primer.worker.yield_resume_registry import ResumeContext, has_resume_hook


def _defs():
    return [
        ExternalToolDef(
            name="lookup_customer",
            description="Look up a customer.",
            args_schema={"type": "object"},
            timeout_seconds=120.0,
        )
    ]


class _MemStorage:
    def __init__(self):
        self.rows = {}

    async def create(self, row):
        self.rows[row.id] = row
        return row

    async def get(self, rid):
        return self.rows.get(rid)

    async def update(self, row):
        self.rows[row.id] = row
        return row

    async def update_unless(
        self,
        row,
        *,
        field,
        forbidden,
        conn=None,
    ):
        current = self._data.get(row.id)
        if current is None:
            raise NotFoundError(f"no entity with id {row.id!r}")
        if getattr(current, field, None) == forbidden:
            return None
        self._data[row.id] = row
        return row


_SYSTEM = PrincipalRef(type="system", id="test", display="test", source="local")


async def test_provider_lists_defs_as_external_tools():
    p = ExternalToolsetProvider(defs=_defs(), call_storage=_MemStorage())
    tools = [t async for t in p.list_tools(principal=None)]
    assert [t.id for t in tools] == ["lookup_customer"]
    assert tools[0].toolset_id == EXTERNAL_TOOLSET_ID
    assert tools[0].yields is True
    assert p.is_yielding("lookup_customer") is True


async def test_manager_merges_external_tools_bypassing_allowlist():
    mgr = ToolExecutionManager(
        toolset_providers={},
        tools=["system__something"],  # allowlist that does NOT name ours
        external_tools=_defs(),
        external_call_storage=_MemStorage(),
        chat_id="chat-1",
    )
    catalogue = await mgr.list_tools()
    assert any(t.id == "external__lookup_customer" for t in catalogue)


async def test_dispatch_writes_row_and_raises_yield():
    store = _MemStorage()
    mgr = ToolExecutionManager(
        toolset_providers={},
        external_tools=_defs(),
        external_call_storage=store,
        chat_id="chat-1",
        initiated_by=_SYSTEM,
    )
    await mgr.list_tools()
    call = ToolCallPart(
        id="tc-1", name="external__lookup_customer", arguments={"id": "c1"}
    )
    with pytest.raises(YieldToWorker) as ei:
        await mgr.execute(call)
    y = ei.value.yielded
    assert y.tool_name == EXTERNAL_PARK_TOOL_NAME
    assert y.event_key == external_event_key("chat-1", "tc-1")
    assert y.timeout == 120.0
    assert y.resume_metadata["original_call"]["name"] == "lookup_customer"
    rows = list(store.rows.values())
    assert len(rows) == 1
    assert rows[0].status == "pending"
    assert rows[0].chat_id == "chat-1"
    assert rows[0].tool_call_id == "tc-1"
    assert rows[0].timeout_at is not None
    assert y.resume_metadata["external_call_row_id"] == rows[0].id


def _ctx(tcid="tc-1"):
    return ResumeContext(tool_name=EXTERNAL_PARK_TOOL_NAME, tool_call_id=tcid)


def test_resume_hook_registered():
    assert has_resume_hook(EXTERNAL_PARK_TOOL_NAME)


def test_resume_hook_builds_tool_results():
    ok = external_resume_hook(
        {}, {"result": {"customer": "c1"}, "is_error": False}, _ctx()
    )
    assert ok.is_error is False
    assert "customer" in ok.output

    err = external_resume_hook({}, {"result": "boom", "is_error": True}, _ctx())
    assert err.is_error is True
    assert err.output == "boom"

    t = external_resume_hook({}, YieldTimeout(elapsed_seconds=5.0), _ctx())
    assert '"timed_out": true' in t.output and t.is_error is True

    c = external_resume_hook(
        {},
        YieldCancelled(
            reason="superseded by new user message",
            cancelled_at=datetime.now(UTC),
            elapsed_seconds=1.0,
        ),
        _ctx(),
    )
    assert '"cancelled": true' in c.output and c.is_error is True
    assert "superseded" in c.output
