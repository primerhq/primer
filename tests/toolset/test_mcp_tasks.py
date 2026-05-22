"""Tests for the MCP Tasks yielding adapter (M5).

The MCP SDK ships the protocol types but does not yet expose
``tasks.create / tasks.get / tasks.cancel`` as ClientSession helper
methods. The adapter uses ``session.send_request`` directly. Tests
mock the session at that boundary.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import mcp.types as mcp_types
import pytest

from matrix.bus.in_memory import InMemoryEventBus
from matrix.bus.mcp_tasks import McpTaskBridge
from matrix.model.session import (
    AgentSessionBinding,
    Session,
    SessionStatus,
)
from matrix.model.yield_ import (
    ToolContext,
    YieldCancelled,
    YieldTimeout,
    YieldToWorker,
    Yielded,
)
from matrix.scheduler.in_memory import InMemoryScheduler, _LeaseState
from matrix.toolset.mcp import (
    McpToolsetProvider,
    is_mcp_task_tool,
    mcp_task_resume,
)


# ===========================================================================
# Adapter: is_mcp_task_tool helper
# ===========================================================================


class TestIsMcpTaskTool:
    def test_task_required_is_task_tool(self):
        tool = mcp_types.Tool(
            name="t",
            description="d",
            inputSchema={"type": "object"},
            execution=mcp_types.ToolExecution(taskSupport="required"),
        )
        assert is_mcp_task_tool(tool) is True

    def test_task_optional_is_task_tool(self):
        tool = mcp_types.Tool(
            name="t",
            description="d",
            inputSchema={"type": "object"},
            execution=mcp_types.ToolExecution(taskSupport="optional"),
        )
        assert is_mcp_task_tool(tool) is True

    def test_task_forbidden_is_not_task_tool(self):
        tool = mcp_types.Tool(
            name="t",
            description="d",
            inputSchema={"type": "object"},
            execution=mcp_types.ToolExecution(taskSupport="forbidden"),
        )
        assert is_mcp_task_tool(tool) is False

    def test_no_execution_field_is_not_task_tool(self):
        tool = mcp_types.Tool(
            name="t",
            description="d",
            inputSchema={"type": "object"},
        )
        assert is_mcp_task_tool(tool) is False


# ===========================================================================
# McpToolsetProvider.call() with task support
# ===========================================================================


class _FakeSession:
    """Stand-in MCP ClientSession used by tests.

    Returns canned responses for list_tools/call_tool. Records every
    ``send_request`` it sees so tests can assert against the wire calls.
    """

    def __init__(
        self,
        *,
        tools: list[mcp_types.Tool],
        task_id: str = "task-1",
    ) -> None:
        self._tools = tools
        self._task_id = task_id
        self.send_request_calls: list = []

    async def list_tools(self):
        return mcp_types.ListToolsResult(tools=self._tools)

    async def call_tool(
        self, name, arguments=None, *, meta=None,
        read_timeout_seconds=None, progress_callback=None,
    ):
        # When task support is requested, the server returns a
        # CallToolResult whose meta carries the task reference.
        if meta and "task" in (meta or {}):
            return mcp_types.CallToolResult(
                content=[],
                isError=False,
                _meta={"task": {"taskId": self._task_id, "status": "working"}},
            )
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text="hello")],
            isError=False,
        )

    async def send_request(self, request, result_type, **kwargs):
        self.send_request_calls.append((request, result_type))
        # Unpack the inner request to figure out which mock to return.
        root = request.root
        method = getattr(root, "method", None)
        params = getattr(root, "params", None)
        if method == "tasks/get":
            return mcp_types.GetTaskResult(
                taskId=params.taskId,
                status="completed",
                createdAt=datetime.now(timezone.utc),
                lastUpdatedAt=datetime.now(timezone.utc),
                ttl=60_000,
            )
        if method == "tasks/result":
            # Payload mirrors a normal CallToolResult.
            return mcp_types.GetTaskPayloadResult.model_validate(
                {
                    "_meta": None,
                    "content": [
                        {"type": "text", "text": "task-completed result"}
                    ],
                    "isError": False,
                }
            )
        if method == "tasks/cancel":
            return mcp_types.CancelTaskResult(
                taskId=params.taskId,
                status="cancelled",
                createdAt=datetime.now(timezone.utc),
                lastUpdatedAt=datetime.now(timezone.utc),
                ttl=60_000,
            )
        raise AssertionError(f"unexpected send_request method: {method!r}")


class _FakeStdioProvider(McpToolsetProvider):
    """Provider subclass that bypasses the real transport.

    Tests inject the session via ``_open_session`` override. Mirrors
    the test pattern in ``tests/toolset/test_mcp.py``.
    """

    def __init__(self, session, *, toolset_id="t-mcp"):
        from matrix.model.provider import (
            McpConfig, StdioConfig, TransportType,
        )
        super().__init__(
            toolset_id=toolset_id,
            config=McpConfig(
                transport=TransportType.STDIO,
                config=StdioConfig(command=["echo", "hi"]),
            ),
        )
        self._fake_session = session

    def _open_session(self, *, principal=None):  # type: ignore[override]
        # Mimic the asynccontextmanager surface of the base class.
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def cm():
            yield self._fake_session

        return cm()


@pytest.mark.asyncio
class TestCallReturnsYieldedForTaskTool:
    async def test_task_tool_returns_yielded_when_ctx_supplied(self):
        tool = mcp_types.Tool(
            name="long_running",
            description="d",
            inputSchema={"type": "object"},
            execution=mcp_types.ToolExecution(taskSupport="required"),
        )
        session = _FakeSession(tools=[tool], task_id="t-abc")
        provider = _FakeStdioProvider(session, toolset_id="srv-1")
        ctx = ToolContext(
            tool_call_id="tc-1",
            session_id="sess-1",
            workspace_id=None,
        )
        # First, populate the task-tool cache.
        async for _ in provider.list_tools():
            pass
        with pytest.raises(YieldToWorker) as exc_info:
            await provider.call(
                tool_name="long_running",
                arguments={"x": 1},
                ctx=ctx,
            )
        y = exc_info.value.yielded
        assert isinstance(y, Yielded)
        # Yielded.tool_name uses the synthetic __mcp_task__ name so a
        # single resume hook services every MCP task. The user-facing
        # tool name lives in resume_metadata.tool_name.
        assert y.tool_name == "__mcp_task__"
        assert y.event_key == "mcp_task:srv-1:t-abc"
        assert y.resume_metadata["task_id"] == "t-abc"
        assert y.resume_metadata["toolset_id"] == "srv-1"
        assert y.resume_metadata["tool_name"] == "long_running"

    async def test_non_task_tool_returns_tool_call_result(self):
        tool = mcp_types.Tool(
            name="echo",
            description="d",
            inputSchema={"type": "object"},
        )
        session = _FakeSession(tools=[tool])
        provider = _FakeStdioProvider(session, toolset_id="srv-2")
        async for _ in provider.list_tools():
            pass
        result = await provider.call(
            tool_name="echo",
            arguments={"x": 1},
            ctx=ToolContext(
                tool_call_id="tc-2",
                session_id="sess-2",
                workspace_id=None,
            ),
        )
        # Returned a regular result — no yield.
        assert result.is_error is False
        assert "hello" in result.output

    async def test_task_tool_without_ctx_still_works_synchronously(self):
        # Backward compat: if no ctx is supplied (legacy caller), we
        # cannot yield — we must invoke the tool synchronously even if
        # it advertises task support.
        tool = mcp_types.Tool(
            name="long_running",
            description="d",
            inputSchema={"type": "object"},
            execution=mcp_types.ToolExecution(taskSupport="optional"),
        )
        session = _FakeSession(tools=[tool])
        provider = _FakeStdioProvider(session, toolset_id="srv-3")
        async for _ in provider.list_tools():
            pass
        result = await provider.call(
            tool_name="long_running",
            arguments={},
        )
        assert result.is_error is False


# ===========================================================================
# mcp_task_resume hook
# ===========================================================================


class TestMcpTaskResumeHook:
    def test_resume_with_real_result_returns_payload(self):
        meta = {
            "task_id": "t-1",
            "toolset_id": "srv",
            "tool_name": "lr",
        }
        payload = {"result": {"answer": 42}}
        result = mcp_task_resume(meta, payload)
        assert result.is_error is False
        body = json.loads(result.output)
        assert body == {"answer": 42}

    def test_resume_with_timeout(self):
        meta = {"task_id": "t-1", "toolset_id": "s", "tool_name": "n"}
        result = mcp_task_resume(meta, YieldTimeout(elapsed_seconds=600.0))
        body = json.loads(result.output)
        assert body["timed_out"] is True
        assert body["task_id"] == "t-1"
        assert body["elapsed_seconds"] == 600.0

    def test_resume_with_cancelled(self):
        meta = {"task_id": "t-1", "toolset_id": "s", "tool_name": "n"}
        result = mcp_task_resume(
            meta,
            YieldCancelled(
                reason="operator stopped",
                cancelled_at=datetime.now(timezone.utc),
                elapsed_seconds=5.0,
            ),
        )
        body = json.loads(result.output)
        assert body["cancelled"] is True
        assert body["reason"] == "operator stopped"
        assert body["task_id"] == "t-1"

    def test_resume_with_error_payload_marks_is_error(self):
        meta = {"task_id": "t-1", "toolset_id": "s", "tool_name": "n"}
        payload = {
            "result": {
                "isError": True,
                "content": [{"type": "text", "text": "task failed"}],
            }
        }
        result = mcp_task_resume(meta, payload)
        assert result.is_error is True


def test_mcp_task_resume_hook_is_registered():
    import matrix.toolset.mcp  # noqa: F401
    from matrix.worker.yield_resume_registry import get_resume_hook

    hook = get_resume_hook("__mcp_task__")
    assert callable(hook)


# ===========================================================================
# McpTaskBridge — polls parked mcp_task:* sessions and publishes
# ===========================================================================


def _make_mcp_task_parked_session(
    *,
    session_id: str,
    tool_call_id: str,
    toolset_id: str,
    task_id: str,
) -> Session:
    now = datetime.now(timezone.utc)
    sess = Session(
        id=session_id,
        workspace_id="ws-x",
        binding=AgentSessionBinding(kind="agent", agent_id="ag-x"),
        status=SessionStatus.RUNNING,
        created_at=now,
    )
    event_key = f"mcp_task:{toolset_id}:{task_id}"
    sess.parked_status = "parked"
    sess.parked_event_key = event_key
    sess.parked_until = now + timedelta(seconds=600)
    sess.parked_at = now
    sess.parked_state = {
        "schema_version": 1,
        "tool_call_id": tool_call_id,
        "yielded": {
            "tool_name": "__mcp_task__",
            "event_key": event_key,
            "timeout": 600.0,
            "resume_metadata": {
                "task_id": task_id,
                "toolset_id": toolset_id,
                "tool_name": "long_running",
            },
        },
        "llm_messages": [],
        "turn_no": 1,
        "started_at": now.isoformat(),
        "resume_event_payload": None,
    }
    return sess


class _MockProviderRegistry:
    """Resolve toolset_id → provider for the bridge."""

    def __init__(self, mapping: dict):
        self._mapping = mapping

    async def get_toolset(self, toolset_id: str):
        provider = self._mapping.get(toolset_id)
        if provider is None:
            from matrix.model.except_ import NotFoundError
            raise NotFoundError(f"toolset {toolset_id!r} not found")
        return provider


@pytest.mark.asyncio
class TestMcpTaskBridge:
    async def test_bridge_polls_and_publishes_on_completion(self):
        bus = InMemoryEventBus()
        await bus.initialize()
        scheduler = InMemoryScheduler()
        await scheduler.initialize()
        await scheduler.register_worker(
            worker_id="wrk", host="h", pid=1, capacity=1,
        )

        sess = _make_mcp_task_parked_session(
            session_id="sess-1",
            tool_call_id="tc-1",
            toolset_id="srv-1",
            task_id="t-abc",
        )
        scheduler._sessions["sess-1"] = sess
        scheduler._leases["sess-1"] = _LeaseState(
            worker_id=None, expires_at=None, runnable=False,
            next_attempt_at=datetime.now(timezone.utc),
        )

        # The session reports completed immediately; the bridge then
        # fetches the result and publishes.
        tool = mcp_types.Tool(
            name="long_running",
            description="d",
            inputSchema={"type": "object"},
            execution=mcp_types.ToolExecution(taskSupport="required"),
        )
        session = _FakeSession(tools=[tool], task_id="t-abc")
        provider = _FakeStdioProvider(session, toolset_id="srv-1")
        registry = _MockProviderRegistry({"srv-1": provider})

        sub = bus.subscribe()
        bridge = McpTaskBridge(
            bus=bus,
            scheduler=scheduler,
            provider_registry=registry,
            poll_seconds=0.05,
        )
        bridge.start()
        try:
            event = await asyncio.wait_for(anext(sub), timeout=2.0)
            assert event.event_key == "mcp_task:srv-1:t-abc"
            # The published payload carries {"result": <CallToolResult-shaped>}
            assert "result" in event.payload
            payload = event.payload["result"]
            # Carry through CallToolResult fields
            assert payload.get("isError") is False
        finally:
            await sub.aclose()
            await bridge.stop()
            await scheduler.aclose()
            await bus.aclose()

    async def test_bridge_ignores_non_mcp_task_parks(self):
        bus = InMemoryEventBus()
        await bus.initialize()
        scheduler = InMemoryScheduler()
        await scheduler.initialize()
        await scheduler.register_worker(
            worker_id="w", host="h", pid=1, capacity=1,
        )
        # ask_user park — bridge must ignore.
        sess = Session(
            id="sess-A",
            workspace_id="ws",
            binding=AgentSessionBinding(kind="agent", agent_id="ag"),
            status=SessionStatus.RUNNING,
            created_at=datetime.now(timezone.utc),
        )
        sess.parked_status = "parked"
        sess.parked_event_key = "ask_user:sess-A:tc"
        sess.parked_until = datetime.now(timezone.utc) + timedelta(seconds=30)
        sess.parked_at = datetime.now(timezone.utc)
        sess.parked_state = {
            "schema_version": 1,
            "tool_call_id": "tc",
            "yielded": {
                "tool_name": "ask_user",
                "event_key": "ask_user:sess-A:tc",
                "timeout": 30.0,
                "resume_metadata": {"prompt": "?"},
            },
            "llm_messages": [],
            "turn_no": 1,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "resume_event_payload": None,
        }
        scheduler._sessions["sess-A"] = sess
        scheduler._leases["sess-A"] = _LeaseState(
            worker_id=None, expires_at=None, runnable=False,
            next_attempt_at=datetime.now(timezone.utc),
        )
        registry = _MockProviderRegistry({})
        sub = bus.subscribe()
        bridge = McpTaskBridge(
            bus=bus,
            scheduler=scheduler,
            provider_registry=registry,
            poll_seconds=0.05,
        )
        bridge.start()
        try:
            # Wait long enough that the bridge has ticked a few times.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(anext(sub), timeout=0.3)
        finally:
            await sub.aclose()
            await bridge.stop()
            await scheduler.aclose()
            await bus.aclose()
