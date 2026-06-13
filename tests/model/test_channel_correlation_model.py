from primer.model.channel_correlation import ChannelCorrelation


def test_session_correlation():
    c = ChannelCorrelation(channel_id="ch-1", anchor="thread-1", kind="session",
                           workspace_id="ws-1", session_id="s-1", tool_call_id="tc-1")
    assert c.id.startswith("channel-correlation-")
    assert c.kind == "session"


def test_chat_correlation():
    c = ChannelCorrelation(channel_id="ch-1", anchor="thread-2", kind="chat", chat_id="chat-1")
    assert c.chat_id == "chat-1"
