"""Notifying-class routing and dispatch through ToolExecutionManager.

S3 spec section 3: a notifying call is answered by the runner itself with
a successful synthetic tool_result; the park machinery is never entered.
"""

from __future__ import annotations

from typing import Any

from primer.agent.tool_manager import ToolExecutionManager
from primer.model.chat import NOTIFYING_TOOL_RESULT, Tool, ToolCallPart, ToolCallResult
from primer.model.principal import PrincipalRef
from primer.model.yield_ import ToolContext, Yielded, YieldToWorker
from primer.toolset.internal import InternalToolsetProvider


_SYSTEM = PrincipalRef(type="system", id="test", display="test", source="local")

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": True,
}


def _tool(name: str, *, tool_class: str = "standard") -> Tool:
    return Tool(
        id=name,
        toolset_id="t1",
        description="d",
        args_schema=_SCHEMA,
        tool_class=tool_class,
        required_role="user",
    )


async def _plain_handler(arguments: dict[str, Any]) -> ToolCallResult:
    del arguments
    return ToolCallResult(output="ran", is_error=False)


async def _parking_handler(
    arguments: dict[str, Any], *, ctx: ToolContext | None = None
) -> ToolCallResult:
    del arguments
    assert ctx is not None
    raise YieldToWorker(
        Yielded(tool_name="notify_me", event_key="k", resume_metadata={}),
        tool_call_id=ctx.tool_call_id,
    )


class _FakeAgentSession:
    """Bare-minimum AgentSession stand-in (tests/agent/test_tool_manager.py:262).

    Pinned decision 12: chat_id is retired by S1 P7, and
    ToolExecutionManager has no session_id parameter, so the session shape
    is a workspace_session object. It is what builds the ToolContext the
    parking fixtures below assert on.
    """

    workspace_id = "ws-1"
    session_id = "sess-1"
    agent_id = "agent-1"
    workspace_tools: list = []


def _manager(registry: dict) -> ToolExecutionManager:
    return ToolExecutionManager(
        toolset_providers={"t1": InternalToolsetProvider("t1", registry)},
        workspace_session=_FakeAgentSession(),  # type: ignore[arg-type]
        initiated_by=_SYSTEM,
    )


async def test_is_notifying_keys_on_the_scoped_id() -> None:
    mgr = _manager(
        {
            "notify_me": (_tool("notify_me", tool_class="notifying"), _plain_handler),
            "do_it": (_tool("do_it"), _plain_handler),
        }
    )
    await mgr.list_tools()
    assert mgr.is_notifying("t1__notify_me") is True
    assert mgr.is_notifying("t1__do_it") is False
    assert mgr.is_notifying("t1__unknown") is False


async def test_a_tool_hidden_by_the_agent_allowlist_is_not_notifying() -> None:
    """The notifying index is a SUBSET of what the agent may call.

    The routing table is populated for every tool a bound provider
    exposes, allowlisted or not. Indexing beside it would let the runner's
    notifying branch answer a call the agent never registered with a
    synthetic success (and emit a delivery record for it) instead of the
    usual not-registered rejection, which would make the notifying class a
    hole in the agent tool surface. Index beside the visible catalogue.
    """
    import pytest

    from primer.model.except_ import UnsupportedContentError

    mgr = ToolExecutionManager(
        toolset_providers={
            "t1": InternalToolsetProvider(
                "t1",
                {
                    "notify_me": (
                        _tool("notify_me", tool_class="notifying"),
                        _plain_handler,
                    )
                },
            )
        },
        tools=["t1__something_else"],  # an allowlist that hides ours
        workspace_session=_FakeAgentSession(),  # type: ignore[arg-type]
        initiated_by=_SYSTEM,
    )
    assert await mgr.list_tools() == []
    assert mgr.is_notifying("t1__notify_me") is False
    with pytest.raises(UnsupportedContentError):
        await mgr.execute(
            ToolCallPart(id="tc-0", name="t1__notify_me", arguments={})
        )
