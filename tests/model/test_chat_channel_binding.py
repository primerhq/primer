"""ChatChannelBinding + Chat.channel_binding field."""

from __future__ import annotations

from datetime import datetime, timezone

from primer.model.chats import Chat, ChatChannelBinding


def _chat(**kw) -> Chat:
    return Chat(
        id="chat-1", agent_id="agent-x",
        created_at=datetime.now(timezone.utc), **kw,
    )


def test_default_binding_is_none():
    assert _chat().channel_binding is None


def test_single_type_binding_no_thread():
    b = ChatChannelBinding(channel_id="ch-1")
    assert b.channel_id == "ch-1"
    assert b.thread_external_id is None
    c = _chat(channel_binding=b)
    assert c.channel_binding.channel_id == "ch-1"


def test_multi_type_binding_with_thread():
    b = ChatChannelBinding(channel_id="ch-1", thread_external_id="1700.0001")
    c = _chat(channel_binding=b)
    assert c.channel_binding.thread_external_id == "1700.0001"


def test_binding_round_trips_through_json():
    c = _chat(channel_binding=ChatChannelBinding(
        channel_id="ch-1", thread_external_id="t-9"))
    dumped = c.model_dump(mode="json")
    revived = Chat.model_validate(dumped)
    assert revived.channel_binding == c.channel_binding
