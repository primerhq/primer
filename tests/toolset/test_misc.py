import json

import pytest

from primer.toolset.misc import build_misc_toolset
from tests.toolset._desc_conformance import assert_tool_conforms

pytestmark = pytest.mark.asyncio


async def _list_tools(provider):
    return [tool async for tool in provider.list_tools()]


async def test_misc_tools_conform():
    provider = build_misc_toolset()
    count = 0
    async for tool in provider.list_tools():
        assert_tool_conforms(tool)
        count += 1
    assert count == 7


async def test_inform_user_registered_and_non_yielding():
    provider = build_misc_toolset()
    tools = await _list_tools(provider)
    assert any(t.id == "inform_user" for t in tools)
    assert provider.is_yielding("inform_user") is False


async def test_inform_user_calls_ctx_inform_and_reports_delivery():
    provider = build_misc_toolset()

    class _Ctx:
        tool_call_id = "tc"
        session_id = "s"
        workspace_id = "w"
        chat_id = None

        async def inform(self, message):
            return 3

    res = await provider.call(
        tool_name="inform_user",
        arguments={"message": "fyi"},
        principal=None,
        ctx=_Ctx(),
    )
    assert res.is_error is False
    assert json.loads(res.output) == {"delivered_to": 3}


async def test_inform_user_no_sink_returns_zero():
    provider = build_misc_toolset()

    class _Ctx:
        tool_call_id = "tc"
        session_id = "s"
        workspace_id = "w"
        chat_id = None
        inform = None

    res = await provider.call(
        tool_name="inform_user",
        arguments={"message": "fyi"},
        principal=None,
        ctx=_Ctx(),
    )
    assert json.loads(res.output) == {"delivered_to": 0}


async def test_inform_user_with_ctx_none_returns_zero():
    # The MCP dispatch path invokes with ctx=None; the handler must degrade to
    # delivered_to: 0 rather than raising a TypeError on the missing ctx.
    provider = build_misc_toolset()
    res = await provider.call(
        tool_name="inform_user",
        arguments={"message": "fyi"},
        principal=None,
        ctx=None,
    )
    assert res.is_error is False
    assert json.loads(res.output) == {"delivered_to": 0}


async def test_inform_user_is_mcp_eligible_but_not_auto_exposed():
    from primer.mcp.safety import is_exposable

    provider = build_misc_toolset()
    tool = next(
        t for t in await _list_tools(provider) if t.id == "inform_user"
    )
    ok, reason = is_exposable(tool, provider=provider)
    assert ok is True and reason is None
