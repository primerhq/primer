"""Offline unit tests for TelegramChannelAdapter."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from primer.channel.adapter import PromptEnvelope, ResponseEnvelope
from primer.channel.inbox import ChannelInbox
from primer.channel.telegram.adapter import TelegramChannelAdapter
from primer.channel.telegram.connection import TELEGRAM_CONNECTIONS
from primer.channel.telegram.render import compute_tag
from primer.model.channel import (
    Channel, ChannelProvider, ChannelProviderType,
    TelegramChannelProviderConfig,
)


class _CapturingInbox(ChannelInbox):
    def __init__(self) -> None:
        self.received: list[ResponseEnvelope] = []
    async def handle_response(self, env: ResponseEnvelope) -> None:
        self.received.append(env)


class _StubBot:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
    async def send_message(self, **body) -> Any:
        self.sent.append(body)
        return type("M", (), {"message_id": 42})()
    async def get_me(self) -> Any:
        return type("M", (), {"username": "primerbot"})()
    async def get_chat(self, chat_id: int) -> Any:
        return type("C", (), {"id": chat_id, "title": "general"})()
    async def edit_message_text(self, **kwargs) -> Any:
        self.sent.append({"edit": True, **kwargs})


class _StubApp:
    def __init__(self) -> None:
        self.bot = _StubBot()


def _provider() -> ChannelProvider:
    return ChannelProvider(
        id="cp-1", provider=ChannelProviderType.TELEGRAM,
        config=TelegramChannelProviderConfig(
            bot_token=SecretStr("123456:abcdefghijklmnopqrstuvwxyz123456"),
        ),
    )


def _channel() -> Channel:
    return Channel(id="ch-1", provider_id="cp-1",
                   provider=ChannelProviderType.TELEGRAM, external_id="123456789")


@pytest.mark.asyncio
async def test_verify_calls_get_me_and_get_chat(monkeypatch):
    app = _StubApp()
    async def _acquire(_): return app
    async def _release(_): pass
    monkeypatch.setattr(TELEGRAM_CONNECTIONS, "acquire", _acquire)
    monkeypatch.setattr(TELEGRAM_CONNECTIONS, "release", _release)
    inbox = _CapturingInbox()
    adapter = TelegramChannelAdapter(
        provider=_provider(), channel=_channel(), inbox=inbox,
    )
    await adapter.initialize()
    try:
        await adapter.verify()
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_post_ask_user_sends_message_with_token(monkeypatch):
    app = _StubApp()
    async def _acquire(_): return app
    async def _release(_): pass
    monkeypatch.setattr(TELEGRAM_CONNECTIONS, "acquire", _acquire)
    monkeypatch.setattr(TELEGRAM_CONNECTIONS, "release", _release)
    inbox = _CapturingInbox()
    adapter = TelegramChannelAdapter(
        provider=_provider(), channel=_channel(), inbox=inbox,
    )
    await adapter.initialize()
    try:
        env = PromptEnvelope(
            kind="ask_user", workspace_id="ws", session_id="s",
            tool_call_id="tc", prompt="hi", response_schema=None,
            choices=None, timeout_at_iso=None,
        )
        await adapter.post_prompt(env)
    finally:
        await adapter.aclose()
    sent = [s for s in app.bot.sent if "text" in s]
    assert len(sent) == 1
    # No visible correlation token in the body any more; HTML formatted.
    assert "[primer:" not in sent[0]["text"]
    assert sent[0]["parse_mode"] == "HTML"
    assert sent[0]["chat_id"] == 123456789
    # ask_user replies are now correlated via the persistent store, not
    # in-memory _reply_targets (which is only used for the reject-reason path).
    assert adapter.resolve_reply_target(42) is None


@pytest.mark.asyncio
async def test_post_inform_plain_message(monkeypatch):
    app = _StubApp()
    async def _acquire(_): return app
    async def _release(_): pass
    monkeypatch.setattr(TELEGRAM_CONNECTIONS, "acquire", _acquire)
    monkeypatch.setattr(TELEGRAM_CONNECTIONS, "release", _release)
    inbox = _CapturingInbox()
    adapter = TelegramChannelAdapter(
        provider=_provider(), channel=_channel(), inbox=inbox,
    )
    await adapter.initialize()
    try:
        await adapter.post_prompt(PromptEnvelope(
            kind="inform", workspace_id="ws", session_id="s", tool_call_id="",
            prompt="status update", response_schema=None, choices=None,
            timeout_at_iso=None,
        ))
    finally:
        await adapter.aclose()
    sent = [s for s in app.bot.sent if "text" in s]
    assert len(sent) == 1
    assert sent[0]["text"] == "status update"
    # Plain message: no inline keyboard / reply markup.
    assert "reply_markup" not in sent[0]
    # Informs expect no reply: no reply target / tag recorded.
    assert dict(adapter._reply_targets) == {}
    assert dict(adapter._tag_cache) == {}


@pytest.mark.asyncio
async def test_resolve_tag_from_cache_after_post(monkeypatch):
    app = _StubApp()
    async def _acquire(_): return app
    async def _release(_): pass
    monkeypatch.setattr(TELEGRAM_CONNECTIONS, "acquire", _acquire)
    monkeypatch.setattr(TELEGRAM_CONNECTIONS, "release", _release)
    inbox = _CapturingInbox()
    adapter = TelegramChannelAdapter(
        provider=_provider(), channel=_channel(), inbox=inbox,
    )
    await adapter.initialize()
    try:
        env = PromptEnvelope(
            kind="ask_user", workspace_id="ws", session_id="s",
            tool_call_id="tc", prompt="hi", response_schema=None,
            choices=None, timeout_at_iso=None,
        )
        await adapter.post_prompt(env)
        tag = compute_tag(workspace_id="ws", session_id="s", tool_call_id="tc")
        ids = await adapter._resolve_tag(tag)
        assert ids == {"workspace_id": "ws", "session_id": "s", "tool_call_id": "tc"}
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_handle_decision_publishes_envelope(monkeypatch):
    app = _StubApp()
    async def _acquire(_): return app
    async def _release(_): pass
    monkeypatch.setattr(TELEGRAM_CONNECTIONS, "acquire", _acquire)
    monkeypatch.setattr(TELEGRAM_CONNECTIONS, "release", _release)
    inbox = _CapturingInbox()
    adapter = TelegramChannelAdapter(
        provider=_provider(), channel=_channel(), inbox=inbox,
    )
    await adapter.initialize()
    try:
        await adapter._handle_decision(
            workspace_id="ws", session_id="s", tool_call_id="tc",
            decision="rejected", reason="no thanks",
            user_id=1234,
        )
    finally:
        await adapter.aclose()
    assert len(inbox.received) == 1
    env = inbox.received[0]
    assert env.kind == "tool_approval"
    assert env.decision == "rejected"
    assert env.reason == "no thanks"


def test_bounded_dict_evicts_oldest():
    from primer.channel.telegram.adapter import _BoundedDict
    d = _BoundedDict(maxsize=3)
    for i in range(5):
        d[i] = i
    assert list(d.keys()) == [2, 3, 4]  # oldest (0,1) evicted
    # re-inserting refreshes recency
    d[2] = "x"
    d[5] = 5
    assert list(d.keys()) == [3, 4, 2, 5][-3:]  # 3 evicted next, 2 kept (refreshed)


def test_adapter_caches_are_bounded():
    from primer.channel.telegram.adapter import (
        _BoundedDict, _CACHE_MAXSIZE, TelegramChannelAdapter,
    )
    a = TelegramChannelAdapter(provider=_provider(), channel=_channel(), inbox=None)
    assert isinstance(a._tag_cache, _BoundedDict)
    assert isinstance(a._reply_targets, _BoundedDict)
    assert a._tag_cache._maxsize == _CACHE_MAXSIZE
