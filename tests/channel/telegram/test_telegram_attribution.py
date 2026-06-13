"""TelegramChannelAdapter prepends attribution header on gate posts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from primer.channel.adapter import PromptEnvelope
from primer.channel.telegram.adapter import TelegramChannelAdapter


class _FakeBot:
    def __init__(self):
        self.send_calls: list[dict] = []

    async def send_message(self, **kw):
        self.send_calls.append(kw)
        return SimpleNamespace(message_id=1)


def _make_envelope(kind: str = "ask_user", **extra) -> PromptEnvelope:
    return PromptEnvelope(
        kind=kind,
        workspace_id="ws-1",
        session_id="sess-1",
        tool_call_id="tc-1",
        prompt="Do the thing",
        response_schema=None,
        choices=None,
        timeout_at_iso=None,
        **extra,
    )


def _adapter() -> TelegramChannelAdapter:
    a = TelegramChannelAdapter(
        provider=SimpleNamespace(id="cp"),
        channel=SimpleNamespace(id="ch-1", external_id="555"),
        inbox=None,
    )
    a._app = SimpleNamespace(bot=_FakeBot())
    return a


@pytest.mark.asyncio
async def test_ask_user_contains_attribution():
    a = _adapter()
    env = _make_envelope(workspace_name="Ops", session_label="s-1")
    await a.post_prompt(env)
    assert a._app.bot.send_calls, "expected send_message to be called"
    text = a._app.bot.send_calls[-1]["text"]
    assert "Workspace: Ops" in text
    assert "Session: s-1" in text


@pytest.mark.asyncio
async def test_no_attribution_when_fields_absent():
    a = _adapter()
    env = _make_envelope()
    await a.post_prompt(env)
    text = a._app.bot.send_calls[-1]["text"]
    assert "Workspace:" not in text
    assert "Session:" not in text


@pytest.mark.asyncio
async def test_post_chat_message_no_attribution():
    a = _adapter()
    await a.post_chat_message("hi")
    text = a._app.bot.send_calls[-1]["text"]
    assert "Workspace:" not in text
