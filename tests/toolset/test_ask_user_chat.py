"""ask_user yields for a chat (ctx.chat_id set, session_id None) and is byte-for-byte
unchanged for a session (ctx.session_id set)."""
from __future__ import annotations

import pytest

from primer.model.yield_ import Yielded, ToolContext


@pytest.mark.asyncio
async def test_ask_user_yields_for_chat():
    from primer.toolset.system import _ask_user_handler
    ctx = ToolContext(tool_call_id="tc1", session_id=None, workspace_id=None, chat_id="chat-1")
    result = await _ask_user_handler({"prompt": "Which env?"}, ctx=ctx)
    assert isinstance(result, Yielded)
    assert result.event_key == "ask_user:chat-1:tc1"
    assert result.resume_metadata["prompt"] == "Which env?"


@pytest.mark.asyncio
async def test_ask_user_session_path_unchanged():
    from primer.toolset.system import _ask_user_handler
    ctx = ToolContext(tool_call_id="tc1", session_id="sess-1", workspace_id="w1")
    result = await _ask_user_handler({"prompt": "Which env?"}, ctx=ctx)
    assert isinstance(result, Yielded)
    assert result.event_key == "ask_user:sess-1:tc1"  # session id wins, unchanged


@pytest.mark.asyncio
async def test_ask_user_errors_when_no_id():
    from primer.toolset.system import _ask_user_handler
    ctx = ToolContext(tool_call_id="tc1", session_id=None, workspace_id=None, chat_id=None)
    result = await _ask_user_handler({"prompt": "x"}, ctx=ctx)
    assert not isinstance(result, Yielded)  # error result, not a Yielded


@pytest.mark.asyncio
async def test_ask_user_stamps_a_fresh_gate_id_per_yield_even_for_a_repeated_tool_call_id():
    """A provider repeats "tc1" across rounds; the gate id is what tells one ask_user from the next (C-033)."""
    import re

    from primer.toolset.system import _ask_user_handler
    ctx = ToolContext(tool_call_id="tc1", session_id="sess-1", workspace_id="w1")
    first = await _ask_user_handler({"prompt": "Which env?"}, ctx=ctx)
    second = await _ask_user_handler({"prompt": "Which env?"}, ctx=ctx)
    assert first.event_key == second.event_key == "ask_user:sess-1:tc1"
    ids = {first.resume_metadata["gate_id"], second.resume_metadata["gate_id"]}
    assert len(ids) == 2 and all(re.fullmatch(r"[0-9a-f]{32}", i) for i in ids)
