"""Phase 2 sweep: slack chat-inbound + commands + blocks + streaming import."""

from __future__ import annotations

from primer.channel.commands import CommandResult
from primer.channel.slack.blocks import build_agent_select_blocks
from primer.channel.slack.commands import handle_slash_command
from primer.channel.slack.streaming import stream_or_post


def test_phase2_surface_present():
    assert callable(handle_slash_command)
    assert callable(stream_or_post)
    blocks = build_agent_select_blocks(
        result=CommandResult(kind="agent_picker", items=[
            {"agent_id": "a", "label": "A"}]), chat_id="chat-1")
    assert blocks[0]["accessory"]["action_id"] == "pick_agent"
