"""ask_user yields for a chat (ctx.chat_id set, session_id None) and is byte-for-byte
unchanged for a session (ctx.session_id set)."""
from __future__ import annotations

import pytest

from primer.model.yield_ import Yielded, ToolContext


@pytest.mark.asyncio
async def test_ask_user_yields_for_chat():
    from primer.toolset.misc import _ask_user_handler
    ctx = ToolContext(tool_call_id="tc1", session_id=None, workspace_id=None, chat_id="chat-1")
    result = await _ask_user_handler({"prompt": "Which env?"}, ctx=ctx)
    assert isinstance(result, Yielded)
    assert result.event_key == "ask_user:chat-1:tc1"
    assert result.resume_metadata["prompt"] == "Which env?"


@pytest.mark.asyncio
async def test_ask_user_session_path_unchanged():
    from primer.toolset.misc import _ask_user_handler
    ctx = ToolContext(tool_call_id="tc1", session_id="sess-1", workspace_id="w1")
    result = await _ask_user_handler({"prompt": "Which env?"}, ctx=ctx)
    assert isinstance(result, Yielded)
    assert result.event_key == "ask_user:sess-1:tc1"  # session id wins, unchanged


@pytest.mark.asyncio
async def test_ask_user_errors_when_no_id():
    from primer.toolset.misc import _ask_user_handler
    ctx = ToolContext(tool_call_id="tc1", session_id=None, workspace_id=None, chat_id=None)
    result = await _ask_user_handler({"prompt": "x"}, ctx=ctx)
    assert not isinstance(result, Yielded)  # error result, not a Yielded
