"""Paginated chat picker for native Slack /agent + handle_slash_command."""

from __future__ import annotations

import pytest

from primer.channel.slack.blocks import CHATS_PER_PAGE, build_chat_select_blocks


def _chats(n):
    return [{"chat_id": f"c{i}", "title": f"chat {i}", "agent_id": "ag"} for i in range(n)]


def test_chat_picker_first_page_has_next_only():
    blocks = build_chat_select_blocks(_chats(20), page=0)
    sel = blocks[0]["accessory"]
    assert sel["action_id"] == "pick_chat_agent"
    assert len(sel["options"]) == CHATS_PER_PAGE
    assert sel["options"][0]["value"] == "c0"
    nav = [e["action_id"] for e in blocks[1]["elements"]]
    assert nav == ["chat_page_next"]  # page 0: next only
    assert blocks[1]["elements"][0]["value"] == "1"


def test_chat_picker_middle_page_has_prev_and_next():
    blocks = build_chat_select_blocks(_chats(20), page=1)
    nav = [e["action_id"] for e in blocks[1]["elements"]]
    assert nav == ["chat_page_prev", "chat_page_next"]


def test_chat_picker_last_page_prev_only():
    blocks = build_chat_select_blocks(_chats(20), page=2)  # 20 -> pages 0,1,2
    sel = blocks[0]["accessory"]
    assert len(sel["options"]) == 20 - 2 * CHATS_PER_PAGE
    nav = [e["action_id"] for e in blocks[1]["elements"]]
    assert nav == ["chat_page_prev"]


def test_single_page_no_nav():
    blocks = build_chat_select_blocks(_chats(3), page=0)
    assert len(blocks) == 1  # no actions block


@pytest.mark.asyncio
async def test_slash_agent_returns_chat_picker(tmp_path):
    from datetime import datetime, timezone
    from primer.channel.slack.commands import handle_slash_command
    from primer.model.chats import Chat, ChatChannelBinding
    from primer.model.provider import SqliteConfig
    from primer.storage.sqlite import SqliteStorageProvider
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "r.sqlite"))
    await p.initialize()
    await p.get_storage(Chat).create(Chat(
        id="chat-1", agent_id="ag", created_at=datetime.now(timezone.utc),
        title="hello", channel_binding=ChatChannelBinding(
            channel_id="ch-sl", thread_external_id="t1")))
    res = await handle_slash_command(
        storage_provider=p, command="/agent", text="", channel_id="ch-sl",
        thread_ts=None)
    assert res.kind == "chat_picker"
    assert [c["chat_id"] for c in res.items] == ["chat-1"]
