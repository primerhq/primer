"""The server-defined client toolset (S3 spec sections 4 and 5).

v1 vocabulary is open_file only, declared notifying, versioned with the
console. No schema ever travels from the browser.
"""

from __future__ import annotations

from primer.agent.tool_manager import ToolExecutionManager
from primer.model.chat import NOTIFYING_TOOL_RESULT, ToolCallPart
from primer.model.principal import PrincipalRef
from primer.toolset.client import CLIENT_TOOLSET_ID, ClientToolsetProvider


_SYSTEM = PrincipalRef(type="system", id="test", display="test", source="local")


class _FakeAgentSession:
    """Bare-minimum AgentSession stand-in (pinned decision 12).

    chat_id is retired by S1 P7 and ToolExecutionManager has no session_id
    parameter, so managers in this suite carry a workspace_session.
    """

    workspace_id = "ws-1"
    session_id = "sess-1"
    agent_id = "agent-1"
    workspace_tools: list = []


async def test_vocabulary_is_open_file_only_and_notifying() -> None:
    provider = ClientToolsetProvider()
    tools = [t async for t in provider.list_tools(principal=None)]
    assert [t.id for t in tools] == ["open_file"]
    assert tools[0].toolset_id == CLIENT_TOOLSET_ID
    assert tools[0].tool_class == "notifying"
    assert tools[0].yields is False
    assert provider.is_yielding("open_file") is False
    schema = tools[0].args_schema
    assert "path" in schema["properties"]
    assert "line" in schema["properties"]
    assert schema["required"] == ["path"]


async def test_call_is_a_no_op_acknowledgement() -> None:
    provider = ClientToolsetProvider()
    ok = await provider.call(
        tool_name="open_file", arguments={"path": "a.txt"}, ctx=None
    )
    assert ok.is_error is False
    assert ok.output == NOTIFYING_TOOL_RESULT
    bad = await provider.call(tool_name="nope", arguments={}, ctx=None)
    assert bad.is_error is True


async def test_client_tools_bypass_the_agent_allowlist() -> None:
    mgr = ToolExecutionManager(
        toolset_providers={CLIENT_TOOLSET_ID: ClientToolsetProvider()},
        tools=["misc__get_datetime"],  # an allowlist that does NOT name ours
        workspace_session=_FakeAgentSession(),  # type: ignore[arg-type]
        initiated_by=_SYSTEM,
    )
    catalogue = await mgr.list_tools()
    assert any(t.id == "client__open_file" for t in catalogue)
    assert mgr.is_notifying("client__open_file") is True
    rp = await mgr.deliver_notifying(
        ToolCallPart(id="tc-1", name="client__open_file", arguments={"path": "a.txt"})
    )
    assert rp.error is False
    assert rp.output == NOTIFYING_TOOL_RESULT
