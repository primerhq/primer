import pytest

from primer.agent.inform import SessionInformSink


@pytest.mark.asyncio
async def test_session_inform_sink_dispatches_inform_envelope():
    sent = {}
    class _Disp:
        async def dispatch_prompt(self, *, envelope):
            sent["env"] = envelope
            return [{"ok": True}, {"ok": True}]
    sink = SessionInformSink(dispatcher=_Disp(), workspace_id="w", session_id="s")
    n = await sink("hello")
    assert n == 2
    assert sent["env"].kind == "inform"
    assert sent["env"].prompt == "hello"
    assert sent["env"].workspace_id == "w" and sent["env"].session_id == "s"


@pytest.mark.asyncio
async def test_session_inform_sink_no_dispatcher_returns_zero():
    sink = SessionInformSink(dispatcher=None, workspace_id="w", session_id="s")
    assert await sink("x") == 0


@pytest.mark.asyncio
async def test_session_inform_sink_counts_only_reached_channels():
    # A failed channel comes back as {"error": ...} and must NOT be counted.
    class _Disp:
        async def dispatch_prompt(self, *, envelope):
            return [{"ok": True}, {"error": "boom"}]
    sink = SessionInformSink(dispatcher=_Disp(), workspace_id="w", session_id="s")
    assert await sink("hi") == 1
