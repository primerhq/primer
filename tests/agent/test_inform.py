import asyncio
from primer.agent.inform import SessionInformSink, ChatInformSink


def test_session_inform_sink_dispatches_inform_envelope():
    sent = {}
    class _Disp:
        async def dispatch_prompt(self, *, envelope):
            sent["env"] = envelope
            return [{"ok": True}, {"ok": True}]
    sink = SessionInformSink(dispatcher=_Disp(), workspace_id="w", session_id="s")
    n = asyncio.get_event_loop().run_until_complete(sink("hello"))
    assert n == 2
    assert sent["env"].kind == "inform"
    assert sent["env"].prompt == "hello"
    assert sent["env"].workspace_id == "w" and sent["env"].session_id == "s"


def test_session_inform_sink_no_dispatcher_returns_zero():
    sink = SessionInformSink(dispatcher=None, workspace_id="w", session_id="s")
    assert asyncio.get_event_loop().run_until_complete(sink("x")) == 0


def test_chat_inform_sink_appends_assistant_line():
    appended = []
    class _Runner:
        async def _append(self, chat, *, kind, payload):
            appended.append((kind, payload)); return None
    sink = ChatInformSink(runner=_Runner(), chat=object())
    n = asyncio.get_event_loop().run_until_complete(sink("hi there"))
    assert n == 1
    assert appended == [("assistant_token", {"delta": "hi there"})]
