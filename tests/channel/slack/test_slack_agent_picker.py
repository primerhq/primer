"""Slack /agent modal: build_agent_switch_modal + submission parsing."""

from __future__ import annotations

from primer.channel.slack.blocks import (
    AGENT_SWITCH_MODAL_CALLBACK_ID,
    build_agent_switch_modal,
    read_agent_switch_submission,
)


def _chats(n):
    return [{"chat_id": f"c{i}", "title": f"chat {i}", "agent_id": "ag"} for i in range(n)]


def _agents():
    return [{"agent_id": "a1", "label": "Agent One"}, {"agent_id": "a2", "label": "Agent Two"}]


def test_modal_has_chat_and_agent_selects():
    v = build_agent_switch_modal(_chats(3), _agents(), channel_external_id="C1")
    assert v["type"] == "modal"
    assert v["callback_id"] == AGENT_SWITCH_MODAL_CALLBACK_ID
    assert v["private_metadata"] == "C1"
    assert v["submit"]["text"] == "Switch"
    block_ids = [b["block_id"] for b in v["blocks"]]
    assert block_ids == ["chat_b", "agent_b"]
    chat_opts = v["blocks"][0]["element"]["options"]
    assert chat_opts[0]["value"] == "c0"
    agent_opts = v["blocks"][1]["element"]["options"]
    assert {o["value"] for o in agent_opts} == {"a1", "a2"}


def test_modal_caps_options_at_100():
    v = build_agent_switch_modal(_chats(150), _agents(), channel_external_id="C1")
    assert len(v["blocks"][0]["element"]["options"]) == 100


def test_no_chats_is_info_modal_without_submit():
    v = build_agent_switch_modal([], _agents(), channel_external_id="C1")
    assert v["type"] == "modal"
    assert "submit" not in v  # info-only: no submit button
    assert "No chats" in v["blocks"][0]["text"]["text"]


def test_no_agents_is_info_modal():
    v = build_agent_switch_modal(_chats(2), [], channel_external_id="C1")
    assert "submit" not in v


def test_read_submission_extracts_chat_and_agent():
    view = {"state": {"values": {
        "chat_b": {"chat_s": {"selected_option": {"value": "chat-7"}}},
        "agent_b": {"agent_s": {"selected_option": {"value": "agent-x"}}},
    }}}
    assert read_agent_switch_submission(view) == ("chat-7", "agent-x")


def test_read_submission_none_for_info_modal():
    assert read_agent_switch_submission({"state": {"values": {}}}) is None
