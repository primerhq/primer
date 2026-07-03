"""Unit tests for the Telegram adapter factory and installed PTB handlers.

Covers ``primer.channel.telegram.factory``: ``_route_channel_event``, the
``_on_callback`` (inline-button) and ``_on_message`` handlers registered on the
PTB Application, and the ``_telegram_factory`` builder. The PTB ``Application``
and the shared-connection registry are faked; adapter methods are mocked so no
real bot / getUpdates poll is used.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("telegram")

from pydantic import SecretStr

from primer.channel.telegram import factory as tg_factory
from primer.model.channel import (
    Channel,
    ChannelProvider,
    ChannelProviderType,
    TelegramChannelProviderConfig,
)


# --------------------------------------------------------------------------- #
# Fakes / helpers
# --------------------------------------------------------------------------- #
def _provider(pid: str = "cp-tg") -> ChannelProvider:
    return ChannelProvider(
        id=pid,
        provider=ChannelProviderType.TELEGRAM,
        config=TelegramChannelProviderConfig(
            bot_token=SecretStr("123456:ABCDEF_this_is_long_enough")),
    )


def _channel(cid: str = "ch-1", ext: str = "100") -> Channel:
    return Channel(
        id=cid, provider_id="cp-tg",
        provider=ChannelProviderType.TELEGRAM, external_id=ext,
    )


class _FakeEntry:
    def __init__(self, adapters: dict | None = None) -> None:
        self.adapters_by_chat_id = adapters or {}


class _FakeRegistry:
    def __init__(self, entry) -> None:
        self._entry = entry

    def entry(self, provider_id):
        return self._entry


class _FakeApp:
    """Captures the PTB handlers registered by ``_install_handlers``."""

    def __init__(self) -> None:
        self.handlers: list = []

    def add_handler(self, handler) -> None:
        self.handlers.append(handler)


_UID = [0]


def _uid() -> str:
    _UID[0] += 1
    return f"telegram-prov-{_UID[0]}"


def _mock_adapter(sp: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        _sp=object() if sp else None,
        _channel=_channel(),
        _reply_targets={},
        _resolve_tag=AsyncMock(),
        _handle_decision=AsyncMock(),
        _handle_text_reply=AsyncMock(),
        remember_reply_target=MagicMock(),
        resolve_reply_target=MagicMock(),
        apply_agent_pick=AsyncMock(),
        build_agent_picker_keyboard=AsyncMock(),
        apply_chat_decision_button=AsyncMock(),
        handle_inbound_chat_media=AsyncMock(),
        handle_inbound_chat_text=AsyncMock(),
    )


def _install(monkeypatch, entry):
    """Install handlers on a FakeApp; return (on_callback, on_message)."""
    pid = _uid()
    monkeypatch.setattr(
        tg_factory, "TELEGRAM_CONNECTIONS", _FakeRegistry(entry))
    app = _FakeApp()
    tg_factory._install_handlers(pid, app)
    on_callback = app.handlers[0].callback
    on_message = app.handlers[1].callback
    return on_callback, on_message


def _context():
    return SimpleNamespace(bot=SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=999)),
        edit_message_text=AsyncMock()))


def _cq(data, chat_id=100, msg_text="orig", mid=5):
    message = SimpleNamespace(
        chat=SimpleNamespace(id=chat_id), text=msg_text, message_id=mid)
    return SimpleNamespace(
        data=data, message=message,
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
        from_user=SimpleNamespace(id=7))


def _msg(text="hi", reply_to=None, chat_id=100, from_id=7, media=None):
    m = SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        from_user=SimpleNamespace(id=from_id, full_name="Full Name"),
        text=text, reply_to_message=reply_to,
        photo=None, document=None, audio=None, voice=None, video=None,
        message_id=5)
    if media:
        setattr(m, media, [object()])
    return m


# --------------------------------------------------------------------------- #
# _route_channel_event
# --------------------------------------------------------------------------- #
def _route_msg():
    entity = SimpleNamespace(type="bot_command", offset=0, length=4)
    return SimpleNamespace(
        chat=SimpleNamespace(id=100, type="group"),
        from_user=SimpleNamespace(id=7, full_name="Full Name"),
        entities=[entity], text="/cmd hi", caption=None, message_id=5)


def _patch_normalizer(monkeypatch, result):
    class _N:
        def __init__(self, provider_id):
            pass

        async def normalize(self, envelope):
            return result

    monkeypatch.setattr(
        "primer.channel.telegram.normalizer.TelegramEventNormalizer", _N)


@pytest.mark.asyncio
async def test_route_channel_event_no_router_returns_false():
    adapter = SimpleNamespace(_event_router=lambda: None, _channel=_channel())
    assert await tg_factory._route_channel_event(adapter, "p", _route_msg()) is False


@pytest.mark.asyncio
async def test_route_channel_event_normalized_none(monkeypatch):
    router = SimpleNamespace(
        has_matching_rule=AsyncMock(return_value=True), route_event=AsyncMock())
    adapter = SimpleNamespace(_event_router=lambda: router, _channel=_channel())
    _patch_normalizer(monkeypatch, None)
    assert await tg_factory._route_channel_event(adapter, "p", _route_msg()) is False
    router.has_matching_rule.assert_not_awaited()


@pytest.mark.asyncio
async def test_route_channel_event_no_match(monkeypatch):
    router = SimpleNamespace(
        has_matching_rule=AsyncMock(return_value=False), route_event=AsyncMock())
    adapter = SimpleNamespace(_event_router=lambda: router, _channel=_channel())
    _patch_normalizer(monkeypatch, {"norm": True})
    assert await tg_factory._route_channel_event(adapter, "p", _route_msg()) is False
    router.route_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_route_channel_event_match_dispatches(monkeypatch):
    router = SimpleNamespace(
        has_matching_rule=AsyncMock(return_value=True), route_event=AsyncMock())
    adapter = SimpleNamespace(_event_router=lambda: router, _channel=_channel())
    _patch_normalizer(monkeypatch, {"norm": True})
    assert await tg_factory._route_channel_event(adapter, "p", _route_msg()) is True
    router.route_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_route_channel_event_swallows_exception(monkeypatch):
    router = SimpleNamespace(
        has_matching_rule=AsyncMock(side_effect=RuntimeError("boom")),
        route_event=AsyncMock())
    adapter = SimpleNamespace(_event_router=lambda: router, _channel=_channel())
    _patch_normalizer(monkeypatch, {"norm": True})
    assert await tg_factory._route_channel_event(adapter, "p", _route_msg()) is False


# --------------------------------------------------------------------------- #
# _on_callback (inline buttons)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_callback_none_is_noop(monkeypatch):
    on_callback, _ = _install(monkeypatch, _FakeEntry())
    # update.callback_query is None -> return without touching the bot.
    await on_callback(SimpleNamespace(callback_query=None), _context())


@pytest.mark.asyncio
async def test_callback_no_entry_is_noop(monkeypatch):
    on_callback, _ = _install(monkeypatch, None)
    cq = _cq("a:TAG")
    await on_callback(SimpleNamespace(callback_query=cq), _context())
    cq.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_callback_unknown_chat_is_noop(monkeypatch):
    adapter = _mock_adapter()
    on_callback, _ = _install(monkeypatch, _FakeEntry({"999": adapter}))
    cq = _cq("a:TAG", chat_id=100)  # chat 100 not registered
    await on_callback(SimpleNamespace(callback_query=cq), _context())
    adapter._handle_decision.assert_not_awaited()


@pytest.mark.asyncio
async def test_callback_approve(monkeypatch):
    adapter = _mock_adapter()
    adapter._resolve_tag = AsyncMock(return_value={
        "workspace_id": "w", "session_id": "s", "tool_call_id": "t"})
    on_callback, _ = _install(monkeypatch, _FakeEntry({"100": adapter}))
    ctx = _context()
    cq = _cq("a:TAG")
    await on_callback(SimpleNamespace(callback_query=cq), ctx)
    adapter._handle_decision.assert_awaited_once()
    kw = adapter._handle_decision.await_args.kwargs
    assert kw["decision"] == "approved"
    assert kw["workspace_id"] == "w"
    assert kw["user_id"] == 7
    ctx.bot.edit_message_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_callback_approve_unresolvable_tag_is_noop(monkeypatch):
    adapter = _mock_adapter()
    adapter._resolve_tag = AsyncMock(return_value=None)
    on_callback, _ = _install(monkeypatch, _FakeEntry({"100": adapter}))
    ctx = _context()
    await on_callback(SimpleNamespace(callback_query=_cq("a:TAG")), ctx)
    adapter._handle_decision.assert_not_awaited()
    ctx.bot.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_callback_reject_sends_reason_prompt(monkeypatch):
    adapter = _mock_adapter()
    ids = {"workspace_id": "w", "session_id": "s", "tool_call_id": "t"}
    adapter._resolve_tag = AsyncMock(return_value=ids)
    on_callback, _ = _install(monkeypatch, _FakeEntry({"100": adapter}))
    ctx = _context()
    await on_callback(SimpleNamespace(callback_query=_cq("r:TAG")), ctx)
    ctx.bot.send_message.assert_awaited_once()
    adapter.remember_reply_target.assert_called_once()
    kw = adapter.remember_reply_target.call_args.kwargs
    assert kw["message_id"] == 999
    assert kw["kind"] == "reject"
    assert kw["ids"] == ids


@pytest.mark.asyncio
async def test_callback_reject_unresolvable_tag_is_noop(monkeypatch):
    adapter = _mock_adapter()
    adapter._resolve_tag = AsyncMock(return_value=None)
    on_callback, _ = _install(monkeypatch, _FakeEntry({"100": adapter}))
    ctx = _context()
    await on_callback(SimpleNamespace(callback_query=_cq("r:TAG")), ctx)
    ctx.bot.send_message.assert_not_awaited()
    adapter.remember_reply_target.assert_not_called()


@pytest.mark.asyncio
async def test_callback_pick_agent_posts_notice(monkeypatch):
    adapter = _mock_adapter()
    adapter.apply_agent_pick = AsyncMock(return_value="Agent switched.")
    on_callback, _ = _install(monkeypatch, _FakeEntry({"100": adapter}))
    ctx = _context()
    await on_callback(
        SimpleNamespace(callback_query=_cq("pick_agent:c1:a1")), ctx)
    adapter.apply_agent_pick.assert_awaited_once()
    ctx.bot.send_message.assert_awaited_once()
    assert ctx.bot.send_message.await_args.kwargs["text"] == "Agent switched."


@pytest.mark.asyncio
async def test_callback_agentpage_navigates(monkeypatch):
    adapter = _mock_adapter()
    adapter.build_agent_picker_keyboard = AsyncMock(
        return_value=[[{"text": "A", "callback_data": "pick_agent:c1:a1"}]])
    on_callback, _ = _install(monkeypatch, _FakeEntry({"100": adapter}))
    cq = _cq("agentpage:c1:2")
    await on_callback(SimpleNamespace(callback_query=cq), _context())
    adapter.build_agent_picker_keyboard.assert_awaited_once()
    assert adapter.build_agent_picker_keyboard.await_args.kwargs["page"] == 2
    cq.edit_message_reply_markup.assert_awaited_once()


@pytest.mark.asyncio
async def test_callback_chat_decision(monkeypatch):
    adapter = _mock_adapter()
    on_callback, _ = _install(monkeypatch, _FakeEntry({"100": adapter}))
    await on_callback(
        SimpleNamespace(callback_query=_cq("chat_ok:c1")), _context())
    adapter.apply_chat_decision_button.assert_awaited_once()


# --------------------------------------------------------------------------- #
# _on_message
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_message_none_is_noop(monkeypatch):
    _, on_message = _install(monkeypatch, _FakeEntry())
    await on_message(SimpleNamespace(message=None), _context())


@pytest.mark.asyncio
async def test_message_no_entry_is_noop(monkeypatch):
    _, on_message = _install(monkeypatch, None)
    await on_message(SimpleNamespace(message=_msg()), _context())


@pytest.mark.asyncio
async def test_message_unknown_chat_is_noop(monkeypatch):
    adapter = _mock_adapter()
    _, on_message = _install(monkeypatch, _FakeEntry({"999": adapter}))
    await on_message(SimpleNamespace(message=_msg(chat_id=100)), _context())
    adapter.handle_inbound_chat_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_message_chat_text_dispatch(monkeypatch):
    adapter = _mock_adapter()
    adapter.handle_inbound_chat_text = AsyncMock(return_value="Chat started.")
    _, on_message = _install(monkeypatch, _FakeEntry({"100": adapter}))
    monkeypatch.setattr(
        tg_factory, "_route_channel_event", AsyncMock(return_value=False))
    ctx = _context()
    await on_message(SimpleNamespace(message=_msg(text="hello")), ctx)
    adapter.handle_inbound_chat_text.assert_awaited_once()
    assert adapter.handle_inbound_chat_text.await_args.kwargs["text"] == "hello"
    ctx.bot.send_message.assert_awaited_once()
    assert ctx.bot.send_message.await_args.kwargs["text"] == "Chat started."


@pytest.mark.asyncio
async def test_message_chat_media_dispatch(monkeypatch):
    adapter = _mock_adapter()
    adapter.handle_inbound_chat_media = AsyncMock(return_value=None)
    _, on_message = _install(monkeypatch, _FakeEntry({"100": adapter}))
    monkeypatch.setattr(
        tg_factory, "_route_channel_event", AsyncMock(return_value=False))
    await on_message(
        SimpleNamespace(message=_msg(text=None, media="photo")), _context())
    adapter.handle_inbound_chat_media.assert_awaited_once()
    adapter.handle_inbound_chat_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_message_rule_match_skips_chat_dispatch(monkeypatch):
    adapter = _mock_adapter()
    _, on_message = _install(monkeypatch, _FakeEntry({"100": adapter}))
    monkeypatch.setattr(
        tg_factory, "_route_channel_event", AsyncMock(return_value=True))
    await on_message(SimpleNamespace(message=_msg(text="trigger")), _context())
    adapter.handle_inbound_chat_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_message_no_reply_without_storage_is_noop(monkeypatch):
    adapter = _mock_adapter(sp=False)
    _, on_message = _install(monkeypatch, _FakeEntry({"100": adapter}))
    await on_message(SimpleNamespace(message=_msg()), _context())
    adapter.handle_inbound_chat_text.assert_not_awaited()
    adapter._handle_text_reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_message_reply_session(monkeypatch):
    adapter = _mock_adapter()
    adapter._reply_targets = {33: {"kind": "reject"}}
    _, on_message = _install(monkeypatch, _FakeEntry({"100": adapter}))
    rec = SimpleNamespace(
        kind="session", workspace_id="w", session_id="s", tool_call_id="t")
    cleared: list = []

    class Corr:
        def __init__(self, sp):
            pass

        async def lookup(self, cid, key):
            return rec

        async def clear(self, cid, key):
            cleared.append(key)

    monkeypatch.setattr("primer.channel.correlation.CorrelationStore", Corr)
    reply = SimpleNamespace(message_id=33)
    msg = _msg(text="my answer", reply_to=reply)
    await on_message(SimpleNamespace(message=msg), _context())
    adapter._handle_text_reply.assert_awaited_once()
    assert adapter._handle_text_reply.await_args.kwargs["text"] == "my answer"
    assert cleared == ["33"]
    assert 33 not in adapter._reply_targets  # popped from in-memory cache


@pytest.mark.asyncio
async def test_message_reply_reject_fallback(monkeypatch):
    adapter = _mock_adapter()
    adapter.resolve_reply_target = MagicMock(return_value={
        "kind": "reject", "workspace_id": "w",
        "session_id": "s", "tool_call_id": "t"})
    _, on_message = _install(monkeypatch, _FakeEntry({"100": adapter}))

    class Corr:
        def __init__(self, sp):
            pass

        async def lookup(self, cid, key):
            return None

    monkeypatch.setattr("primer.channel.correlation.CorrelationStore", Corr)
    reply = SimpleNamespace(message_id=44)
    msg = _msg(text="because reasons", reply_to=reply)
    await on_message(SimpleNamespace(message=msg), _context())
    adapter._handle_decision.assert_awaited_once()
    kw = adapter._handle_decision.await_args.kwargs
    assert kw["decision"] == "rejected"
    assert kw["reason"] == "because reasons"


@pytest.mark.asyncio
async def test_message_reply_lookup_error_falls_through(monkeypatch):
    adapter = _mock_adapter()
    adapter.resolve_reply_target = MagicMock(return_value=None)
    _, on_message = _install(monkeypatch, _FakeEntry({"100": adapter}))

    class Corr:
        def __init__(self, sp):
            pass

        async def lookup(self, cid, key):
            raise RuntimeError("store down")

    monkeypatch.setattr("primer.channel.correlation.CorrelationStore", Corr)
    reply = SimpleNamespace(message_id=66)
    await on_message(
        SimpleNamespace(message=_msg(reply_to=reply)), _context())
    # lookup failed -> rec None -> in-memory fallback consulted (also None).
    adapter.resolve_reply_target.assert_called_once_with(66)
    adapter._handle_decision.assert_not_awaited()


@pytest.mark.asyncio
async def test_message_reply_no_target_is_noop(monkeypatch):
    adapter = _mock_adapter()
    adapter.resolve_reply_target = MagicMock(return_value=None)
    _, on_message = _install(monkeypatch, _FakeEntry({"100": adapter}))

    class Corr:
        def __init__(self, sp):
            pass

        async def lookup(self, cid, key):
            return None

    monkeypatch.setattr("primer.channel.correlation.CorrelationStore", Corr)
    reply = SimpleNamespace(message_id=55)
    await on_message(
        SimpleNamespace(message=_msg(reply_to=reply)), _context())
    adapter._handle_decision.assert_not_awaited()


# --------------------------------------------------------------------------- #
# idempotent install + _telegram_factory
# --------------------------------------------------------------------------- #
def test_install_handlers_is_idempotent(monkeypatch):
    pid = _uid()
    monkeypatch.setattr(
        tg_factory, "TELEGRAM_CONNECTIONS", _FakeRegistry(_FakeEntry()))
    app1 = _FakeApp()
    tg_factory._install_handlers(pid, app1)
    assert len(app1.handlers) == 2
    app2 = _FakeApp()
    tg_factory._install_handlers(pid, app2)  # same pid -> early return
    assert app2.handlers == []


@pytest.mark.asyncio
async def test_telegram_factory_builds_and_installs(monkeypatch):
    created: dict = {}

    class _FakeAdapter:
        def __init__(self, **kw):
            created.update(kw)
            self.initialized = False

        async def initialize(self):
            self.initialized = True

    installed: list = []
    monkeypatch.setattr(tg_factory, "TelegramChannelAdapter", _FakeAdapter)
    monkeypatch.setattr(
        tg_factory, "_install_handlers",
        lambda pid, app: installed.append((pid, app)))
    app_obj = object()
    entry = SimpleNamespace(app=app_obj)
    monkeypatch.setattr(
        tg_factory, "TELEGRAM_CONNECTIONS", _FakeRegistry(entry))

    provider = _provider("cp-x")
    channel = _channel()
    adapter = await tg_factory._telegram_factory(
        provider, channel, object(), storage_provider="SP",
        event_bus="EB", claim_engine="CE", artifact_registry="AR")

    assert isinstance(adapter, _FakeAdapter)
    assert adapter.initialized is True
    assert created["provider"] is provider
    assert created["storage_provider"] == "SP"
    assert installed == [(provider.id, app_obj)]


@pytest.mark.asyncio
async def test_telegram_factory_without_connection_skips_handlers(monkeypatch):
    class _FakeAdapter:
        def __init__(self, **kw):
            pass

        async def initialize(self):
            pass

    installed: list = []
    monkeypatch.setattr(tg_factory, "TelegramChannelAdapter", _FakeAdapter)
    monkeypatch.setattr(
        tg_factory, "_install_handlers", lambda *a: installed.append(a))
    monkeypatch.setattr(
        tg_factory, "TELEGRAM_CONNECTIONS", _FakeRegistry(None))
    await tg_factory._telegram_factory(_provider("cp-y"), _channel(), object())
    assert installed == []
