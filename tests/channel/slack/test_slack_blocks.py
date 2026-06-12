"""Slack static-select agent-picker block + selection -> set_agent."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from primer.channel.commands import CommandResult
from primer.channel.slack.blocks import (
    build_agent_select_blocks, parse_agent_selection,
)
from primer.model.agent import Agent
from primer.model.chats import Chat
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider


def test_build_agent_select_blocks():
    res = CommandResult(kind="agent_picker", items=[
        {"agent_id": "agent-x", "label": "X"},
        {"agent_id": "agent-y", "label": "Y"}])
    blocks = build_agent_select_blocks(result=res, chat_id="chat-1")
    select = blocks[0]["accessory"]
    assert select["type"] == "static_select"
    assert select["action_id"] == "pick_agent"
    opts = select["options"]
    assert {o["value"] for o in opts} == {"chat-1:agent-x", "chat-1:agent-y"}


@pytest.mark.asyncio
async def test_parse_agent_selection_switches(tmp_path: Path):
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "r.sqlite"))
    await p.initialize()
    for aid, nm in [("agent-x", "X"), ("agent-y", "Y")]:
        await p.get_storage(Agent).create(Agent(
            id=aid, description=nm, model={"provider_id": "lp", "model_name": "m"}))
    await p.get_storage(Chat).create(Chat(
        id="chat-1", agent_id="agent-x", created_at=datetime.now(timezone.utc)))
    notice = await parse_agent_selection(
        storage_provider=p, selected_value="chat-1:agent-y")
    assert "Y" in notice
    assert (await p.get_storage(Chat).get("chat-1")).agent_id == "agent-y"
