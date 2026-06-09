"""A chat-driven ToolExecutionManager injects a ToolContext carrying chat_id
(and no session_id) so yielding tools + the approval gate can identify the chat."""
from __future__ import annotations

import pytest

from primer.model.yield_ import ToolContext


def test_tool_context_accepts_chat_id():
    ctx = ToolContext(tool_call_id="tc1", session_id=None, workspace_id=None, chat_id="chat-1")
    assert ctx.chat_id == "chat-1"
    assert ctx.session_id is None


def test_tool_context_chat_id_defaults_none():
    ctx = ToolContext(tool_call_id="tc1", session_id="s1", workspace_id="w1")
    assert ctx.chat_id is None
