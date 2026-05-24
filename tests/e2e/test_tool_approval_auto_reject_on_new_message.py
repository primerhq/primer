"""E2E: chat parks on _approval; client sends new user_message; agent's
next turn sees the synthetic rejection ToolResultPart."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_new_message_auto_rejects_pending_approval() -> None:
    pytest.skip(
        "E2E chat-WS auto-reject journey scheduled to land alongside "
        "the portable stub-LLM harness; unit test "
        "tests/api/test_chat_ws_tool_approval.py covers the auto-reject "
        "publish path (once Task 10 lands)."
    )
