import datetime as _dt

from primer.model.chats import Chat


def _chat(**kw):
    return Chat(id="c1", agent_id="a1",
               created_at=_dt.datetime.now(_dt.timezone.utc), **kw)


def test_chat_pending_tool_call_defaults_none():
    assert _chat().pending_tool_call is None


def test_chat_pending_tool_call_roundtrip():
    c = _chat(pending_tool_call={"tool_call_id": "tc1", "mode": "ask_user"})
    assert c.pending_tool_call["mode"] == "ask_user"
