"""Unit tests for the Slack adapter factory and its installed bolt handlers.

Covers ``primer.channel.slack.factory``: the ``_route_channel_event`` helper,
every handler installed by ``_install_handlers`` (action / view / command /
event), and the ``_slack_factory`` builder. The slack_bolt ``app`` and the
shared-connection registry are faked so no network / real SDK client is used.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("slack_bolt")

from pydantic import SecretStr

from primer.channel.slack import factory as slack_factory
from primer.channel.commands import CommandResult
from primer.model.channel import (
    Channel,
    ChannelProvider,
    ChannelProviderType,
    SlackChannelProviderConfig,
)


# --------------------------------------------------------------------------- #
# Fakes / helpers
# --------------------------------------------------------------------------- #
def _provider(pid: str = "cp-slack") -> ChannelProvider:
    return ChannelProvider(
        id=pid,
        provider=ChannelProviderType.SLACK,
        config=SlackChannelProviderConfig(
            app_token=SecretStr("xapp-1-test"),
            bot_token=SecretStr("xoxb-test"),
        ),
    )


def _channel(cid: str = "ch-1", ext: str = "C123") -> Channel:
    return Channel(
        id=cid, provider_id="cp-slack",
        provider=ChannelProviderType.SLACK, external_id=ext,
    )


class _FakeApp:
    """Captures the bolt handlers registered by ``_install_handlers``."""

    def __init__(self) -> None:
        self.actions: dict[str, object] = {}
        self.views: dict[str, object] = {}
        self.commands: dict[str, object] = {}
        self.events: dict[str, object] = {}

    def action(self, name):
        def deco(fn):
            self.actions[name] = fn
            return fn
        return deco

    def view(self, name):
        def deco(fn):
            self.views[name] = fn
            return fn
        return deco

    def command(self, name):
        def deco(fn):
            self.commands[name] = fn
            return fn
        return deco

    def event(self, name):
        def deco(fn):
            self.events[name] = fn
            return fn
        return deco


class _FakeEntry:
    def __init__(self, adapters: dict | None = None) -> None:
        self.adapters_by_channel_id = adapters or {}


class _FakeRegistry:
    def __init__(self, entry) -> None:
        self._entry = entry

    def entry(self, provider_id):
        return self._entry


_UID = [0]


def _uid() -> str:
    _UID[0] += 1
    return f"slack-prov-{_UID[0]}"


def _mock_adapter(sp: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        _sp=object() if sp else None,
        _channel=_channel(),
        _handle_decision=AsyncMock(),
        _handle_text_reply=AsyncMock(),
        handle_inbound_chat_message=AsyncMock(),
    )


def _install(monkeypatch, entry) -> tuple[str, _FakeApp]:
    """Install handlers on a fresh FakeApp under a unique provider id and point
    the module ``SLACK_CONNECTIONS`` at a registry returning *entry*."""
    pid = _uid()
    monkeypatch.setattr(slack_factory, "SLACK_CONNECTIONS", _FakeRegistry(entry))
    app = _FakeApp()
    slack_factory._install_handlers(pid, app)
    return pid, app


# --------------------------------------------------------------------------- #
# _route_channel_event
# --------------------------------------------------------------------------- #
class _Normalizer:
    result: object = None

    def __init__(self, provider_id):
        self.provider_id = provider_id

    async def normalize(self, envelope):
        return type(self).result


def _patch_normalizer(monkeypatch, result):
    cls = type("N", (_Normalizer,), {"result": result})
    monkeypatch.setattr(
        "primer.channel.slack.normalizer.SlackEventNormalizer", cls)


@pytest.mark.asyncio
async def test_route_channel_event_no_router_returns_false():
    adapter = SimpleNamespace(_event_router=lambda: None, _channel=_channel())
    assert await slack_factory._route_channel_event(adapter, "p", {}) is False


@pytest.mark.asyncio
async def test_route_channel_event_normalized_none(monkeypatch):
    router = SimpleNamespace(
        has_matching_rule=AsyncMock(return_value=True),
        route_event=AsyncMock())
    adapter = SimpleNamespace(_event_router=lambda: router, _channel=_channel())
    _patch_normalizer(monkeypatch, None)
    assert await slack_factory._route_channel_event(adapter, "p", {}) is False
    router.has_matching_rule.assert_not_awaited()


@pytest.mark.asyncio
async def test_route_channel_event_no_matching_rule(monkeypatch):
    router = SimpleNamespace(
        has_matching_rule=AsyncMock(return_value=False),
        route_event=AsyncMock())
    adapter = SimpleNamespace(_event_router=lambda: router, _channel=_channel())
    _patch_normalizer(monkeypatch, {"norm": True})
    assert await slack_factory._route_channel_event(adapter, "p", {}) is False
    router.route_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_route_channel_event_match_dispatches(monkeypatch):
    router = SimpleNamespace(
        has_matching_rule=AsyncMock(return_value=True),
        route_event=AsyncMock())
    adapter = SimpleNamespace(_event_router=lambda: router, _channel=_channel())
    _patch_normalizer(monkeypatch, {"norm": True})
    assert await slack_factory._route_channel_event(adapter, "p", {}) is True
    router.route_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_route_channel_event_swallows_exception(monkeypatch):
    router = SimpleNamespace(
        has_matching_rule=AsyncMock(side_effect=RuntimeError("boom")),
        route_event=AsyncMock())
    adapter = SimpleNamespace(_event_router=lambda: router, _channel=_channel())
    _patch_normalizer(monkeypatch, {"norm": True})
    assert await slack_factory._route_channel_event(adapter, "p", {}) is False


# --------------------------------------------------------------------------- #
# action: approve
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_action_approve_records_decision_and_updates(monkeypatch):
    adapter = _mock_adapter()
    _, app = _install(monkeypatch, _FakeEntry({"C123": adapter}))
    ack = AsyncMock()
    client = SimpleNamespace(chat_update=AsyncMock())
    body = {
        "actions": [{"value": "approve:ws1:sid1:tc1"}],
        "channel": {"id": "C123"},
        "user": {"id": "U9"},
        "message": {"ts": "111.222", "blocks": [{"type": "actions"}]},
    }
    await app.actions["approve"](ack, body, client)
    ack.assert_awaited_once()
    adapter._handle_decision.assert_awaited_once()
    kw = adapter._handle_decision.await_args.kwargs
    assert kw["workspace_id"] == "ws1"
    assert kw["session_id"] == "sid1"
    assert kw["tool_call_id"] == "tc1"
    assert kw["decision"] == "approved"
    assert kw["reason"] is None
    assert kw["user_id"] == "U9"
    client.chat_update.assert_awaited_once()


@pytest.mark.asyncio
async def test_action_approve_malformed_value_is_noop(monkeypatch):
    adapter = _mock_adapter()
    _, app = _install(monkeypatch, _FakeEntry({"C123": adapter}))
    ack = AsyncMock()
    client = SimpleNamespace(chat_update=AsyncMock())
    await app.actions["approve"](ack, {"actions": [{"value": "oops"}]}, client)
    ack.assert_awaited_once()
    adapter._handle_decision.assert_not_awaited()
    client.chat_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_action_approve_unknown_channel_is_noop(monkeypatch):
    _, app = _install(monkeypatch, _FakeEntry({}))
    ack = AsyncMock()
    client = SimpleNamespace(chat_update=AsyncMock())
    body = {
        "actions": [{"value": "approve:w:s:t"}],
        "channel": {"id": "C123"}, "user": {"id": "U"}, "message": {},
    }
    await app.actions["approve"](ack, body, client)
    ack.assert_awaited_once()
    client.chat_update.assert_not_awaited()


# --------------------------------------------------------------------------- #
# action: reject
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_action_reject_opens_reject_modal(monkeypatch):
    _, app = _install(monkeypatch, _FakeEntry({"C123": _mock_adapter()}))
    ack = AsyncMock()
    client = SimpleNamespace(views_open=AsyncMock())
    body = {
        "actions": [{"value": "reject:ws:sid:tc"}],
        "channel": {"id": "C123"},
        "message": {"ts": "9.9"},
        "trigger_id": "trig-1",
    }
    await app.actions["reject"](ack, body, client)
    ack.assert_awaited_once()
    client.views_open.assert_awaited_once()
    view = client.views_open.await_args.kwargs["view"]
    assert view["callback_id"] == slack_factory.REJECT_MODAL_CALLBACK_ID
    assert "reject:ws:sid:tc" in view["private_metadata"]


@pytest.mark.asyncio
async def test_action_reject_malformed_value_is_noop(monkeypatch):
    _, app = _install(monkeypatch, _FakeEntry({"C123": _mock_adapter()}))
    ack = AsyncMock()
    client = SimpleNamespace(views_open=AsyncMock())
    await app.actions["reject"](ack, {"actions": [{"value": "nope"}]}, client)
    client.views_open.assert_not_awaited()


# --------------------------------------------------------------------------- #
# action: pick_agent
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_action_pick_agent_posts_notice(monkeypatch):
    adapter = _mock_adapter()
    _, app = _install(monkeypatch, _FakeEntry({"C123": adapter}))
    monkeypatch.setattr(
        "primer.channel.slack.blocks.parse_agent_selection",
        AsyncMock(return_value="Switched agent to X."))
    ack = AsyncMock()
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    body = {
        "actions": [{"selected_option": {"value": "chat1:agent1"}}],
        "channel": {"id": "C123"},
        "message": {"thread_ts": "tt-1"},
    }
    await app.actions["pick_agent"](ack, body, client)
    client.chat_postMessage.assert_awaited_once()
    kw = client.chat_postMessage.await_args.kwargs
    assert kw["text"] == "Switched agent to X."
    assert kw["thread_ts"] == "tt-1"


@pytest.mark.asyncio
async def test_action_pick_agent_malformed_is_noop(monkeypatch):
    adapter = _mock_adapter()
    _, app = _install(monkeypatch, _FakeEntry({"C123": adapter}))
    ack = AsyncMock()
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await app.actions["pick_agent"](
        ack, {"actions": [{}], "channel": {"id": "C123"}}, client)
    client.chat_postMessage.assert_not_awaited()


@pytest.mark.asyncio
async def test_action_pick_agent_without_storage_is_noop(monkeypatch):
    adapter = _mock_adapter(sp=False)
    _, app = _install(monkeypatch, _FakeEntry({"C123": adapter}))
    ack = AsyncMock()
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    body = {
        "actions": [{"selected_option": {"value": "c:a"}}],
        "channel": {"id": "C123"}, "message": {},
    }
    await app.actions["pick_agent"](ack, body, client)
    client.chat_postMessage.assert_not_awaited()


# --------------------------------------------------------------------------- #
# view: reject-modal submit
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_view_reject_modal_records_and_updates(monkeypatch):
    adapter = _mock_adapter()
    _, app = _install(monkeypatch, _FakeEntry({"C123": adapter}))
    ack = AsyncMock()
    client = SimpleNamespace(
        conversations_history=AsyncMock(
            return_value={"messages": [{"blocks": [{"type": "actions"}]}]}),
        chat_update=AsyncMock())
    view = {
        "private_metadata": "reject:ws:sid:tc:C123:111.2",
        "state": {"values": {"reason": {"reason_text": {"value": "no good"}}}},
    }
    handler = app.views[slack_factory.REJECT_MODAL_CALLBACK_ID]
    await handler(ack, {"user": {"id": "U1"}}, view, client)
    ack.assert_awaited_once()
    adapter._handle_decision.assert_awaited_once()
    kw = adapter._handle_decision.await_args.kwargs
    assert kw["decision"] == "rejected"
    assert kw["reason"] == "no good"
    client.conversations_history.assert_awaited_once()
    client.chat_update.assert_awaited_once()


@pytest.mark.asyncio
async def test_view_reject_modal_short_metadata_is_noop(monkeypatch):
    adapter = _mock_adapter()
    _, app = _install(monkeypatch, _FakeEntry({"C123": adapter}))
    handler = app.views[slack_factory.REJECT_MODAL_CALLBACK_ID]
    await handler(AsyncMock(), {"user": {}},
                  {"private_metadata": "reject:only"}, SimpleNamespace())
    adapter._handle_decision.assert_not_awaited()


@pytest.mark.asyncio
async def test_view_reject_modal_no_entry_is_noop(monkeypatch):
    adapter = _mock_adapter()
    _, app = _install(monkeypatch, None)  # registry.entry -> None
    handler = app.views[slack_factory.REJECT_MODAL_CALLBACK_ID]
    view = {
        "private_metadata": "reject:w:s:t",
        "state": {"values": {"reason": {"reason_text": {"value": "x"}}}},
    }
    await handler(AsyncMock(), {"user": {"id": "U"}}, view, SimpleNamespace())
    adapter._handle_decision.assert_not_awaited()


# --------------------------------------------------------------------------- #
# command: /agent (modal opener)
# --------------------------------------------------------------------------- #
class _FakeExec:
    switch_allowed = True

    def __init__(self, *, storage_provider):
        self.sp = storage_provider

    async def agent_switch_allowed(self, channel_id):
        return type(self).switch_allowed

    async def list_chats(self, *, channel_id):
        return SimpleNamespace(
            items=[{"chat_id": "c1", "title": "Chat 1", "agent_id": "a1"}])

    async def agent_picker(self, *, channel_id):
        return SimpleNamespace(items=[{"agent_id": "a1", "label": "Agent 1"}])


@pytest.mark.asyncio
async def test_command_agent_opens_modal(monkeypatch):
    adapter = _mock_adapter()
    _, app = _install(monkeypatch, _FakeEntry({"C123": adapter}))
    monkeypatch.setattr(
        "primer.channel.commands.CommandExecutor",
        type("E", (_FakeExec,), {"switch_allowed": True}))
    ack = AsyncMock()
    client = SimpleNamespace(views_open=AsyncMock(), chat_postEphemeral=AsyncMock())
    body = {"channel_id": "C123", "trigger_id": "tg-1", "user_id": "U"}
    await app.commands["/agent"](ack, body, client)
    ack.assert_awaited_once()
    client.views_open.assert_awaited_once()
    assert "view" in client.views_open.await_args.kwargs


@pytest.mark.asyncio
async def test_command_agent_disabled_posts_ephemeral(monkeypatch):
    adapter = _mock_adapter()
    _, app = _install(monkeypatch, _FakeEntry({"C123": adapter}))
    monkeypatch.setattr(
        "primer.channel.commands.CommandExecutor",
        type("E", (_FakeExec,), {"switch_allowed": False}))
    ack = AsyncMock()
    client = SimpleNamespace(views_open=AsyncMock(), chat_postEphemeral=AsyncMock())
    body = {"channel_id": "C123", "trigger_id": "tg-1", "user_id": "U7"}
    await app.commands["/agent"](ack, body, client)
    client.views_open.assert_not_awaited()
    client.chat_postEphemeral.assert_awaited_once()


@pytest.mark.asyncio
async def test_command_agent_without_storage_is_noop(monkeypatch):
    adapter = _mock_adapter(sp=False)
    _, app = _install(monkeypatch, _FakeEntry({"C123": adapter}))
    ack = AsyncMock()
    client = SimpleNamespace(views_open=AsyncMock(), chat_postEphemeral=AsyncMock())
    await app.commands["/agent"](
        ack, {"channel_id": "C123", "trigger_id": "t"}, client)
    client.views_open.assert_not_awaited()
    client.chat_postEphemeral.assert_not_awaited()


# --------------------------------------------------------------------------- #
# command: /help  (exercises the shared _run_slash body)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_command_help_posts_notice_text(monkeypatch):
    adapter = _mock_adapter()
    _, app = _install(monkeypatch, _FakeEntry({"C123": adapter}))
    monkeypatch.setattr(
        "primer.channel.slack.commands.handle_slash_command",
        AsyncMock(return_value=CommandResult(kind="notice", text="help!")))
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await app.commands["/help"](AsyncMock(), {"channel_id": "C123", "text": ""}, client)
    client.chat_postMessage.assert_awaited_once()
    assert client.chat_postMessage.await_args.kwargs["text"] == "help!"


@pytest.mark.asyncio
async def test_command_help_renders_chat_list(monkeypatch):
    adapter = _mock_adapter()
    _, app = _install(monkeypatch, _FakeEntry({"C123": adapter}))
    monkeypatch.setattr(
        "primer.channel.slack.commands.handle_slash_command",
        AsyncMock(return_value=CommandResult(
            kind="list",
            items=[{"title": "T", "chat_id": "c9", "agent_id": "a9"}])))
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await app.commands["/help"](AsyncMock(), {"channel_id": "C123"}, client)
    text = client.chat_postMessage.await_args.kwargs["text"]
    assert text.startswith("Chats on this channel:")
    assert "T (c9) -> a9" in text


@pytest.mark.asyncio
async def test_command_help_renders_empty_chat_list(monkeypatch):
    adapter = _mock_adapter()
    _, app = _install(monkeypatch, _FakeEntry({"C123": adapter}))
    monkeypatch.setattr(
        "primer.channel.slack.commands.handle_slash_command",
        AsyncMock(return_value=CommandResult(kind="list", items=[])))
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await app.commands["/help"](AsyncMock(), {"channel_id": "C123"}, client)
    assert client.chat_postMessage.await_args.kwargs["text"] == \
        "No chats yet on this channel."


@pytest.mark.asyncio
async def test_command_help_empty_text_skips_post(monkeypatch):
    adapter = _mock_adapter()
    _, app = _install(monkeypatch, _FakeEntry({"C123": adapter}))
    monkeypatch.setattr(
        "primer.channel.slack.commands.handle_slash_command",
        AsyncMock(return_value=CommandResult(kind="notice", text="")))
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await app.commands["/help"](AsyncMock(), {"channel_id": "C123"}, client)
    client.chat_postMessage.assert_not_awaited()


@pytest.mark.asyncio
async def test_command_help_without_storage_is_noop(monkeypatch):
    adapter = _mock_adapter(sp=False)
    _, app = _install(monkeypatch, _FakeEntry({"C123": adapter}))
    called = AsyncMock()
    monkeypatch.setattr(
        "primer.channel.slack.commands.handle_slash_command", called)
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await app.commands["/help"](AsyncMock(), {"channel_id": "C123"}, client)
    called.assert_not_awaited()
    client.chat_postMessage.assert_not_awaited()


# --------------------------------------------------------------------------- #
# event: message
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_event_message_ignores_bot(monkeypatch):
    adapter = _mock_adapter()
    _, app = _install(monkeypatch, _FakeEntry({"C123": adapter}))
    await app.events["message"]({"bot_id": "B1"}, SimpleNamespace())
    adapter.handle_inbound_chat_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_event_message_ignores_edit_subtype(monkeypatch):
    adapter = _mock_adapter()
    _, app = _install(monkeypatch, _FakeEntry({"C123": adapter}))
    await app.events["message"](
        {"subtype": "message_changed", "channel": "C123"}, SimpleNamespace())
    adapter.handle_inbound_chat_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_event_message_unknown_channel_is_noop(monkeypatch):
    adapter = _mock_adapter()
    _, app = _install(monkeypatch, _FakeEntry({"C123": adapter}))
    await app.events["message"](
        {"channel": "OTHER", "ts": "1"}, SimpleNamespace())
    adapter.handle_inbound_chat_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_event_message_session_reply(monkeypatch):
    adapter = _mock_adapter()
    _, app = _install(monkeypatch, _FakeEntry({"C123": adapter}))
    rec = SimpleNamespace(
        kind="session", workspace_id="w", session_id="s", tool_call_id="t")
    cleared: list[str] = []

    class Corr:
        def __init__(self, sp):
            pass

        async def lookup(self, cid, key):
            return rec

        async def clear(self, cid, key):
            cleared.append(key)

    monkeypatch.setattr("primer.channel.correlation.CorrelationStore", Corr)
    event = {"channel": "C123", "thread_ts": "TT",
             "text": "answer", "user": "U", "ts": "1"}
    await app.events["message"](event, SimpleNamespace())
    adapter._handle_text_reply.assert_awaited_once()
    assert adapter._handle_text_reply.await_args.kwargs["text"] == "answer"
    assert cleared == ["TT"]
    adapter.handle_inbound_chat_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_event_message_chat_dispatch(monkeypatch):
    adapter = _mock_adapter()
    _, app = _install(monkeypatch, _FakeEntry({"C123": adapter}))
    monkeypatch.setattr(
        slack_factory, "_route_channel_event", AsyncMock(return_value=False))
    event = {"channel": "C123", "text": "hello",
             "user": "U2", "ts": "9", "files": None}
    await app.events["message"](event, SimpleNamespace())
    adapter.handle_inbound_chat_message.assert_awaited_once()
    kw = adapter.handle_inbound_chat_message.await_args.kwargs
    assert kw["text"] == "hello"
    assert kw["sender_name"] == "U2"
    assert kw["message_ts"] == "9"


@pytest.mark.asyncio
async def test_event_message_rule_match_skips_chat_dispatch(monkeypatch):
    adapter = _mock_adapter()
    _, app = _install(monkeypatch, _FakeEntry({"C123": adapter}))
    monkeypatch.setattr(
        slack_factory, "_route_channel_event", AsyncMock(return_value=True))
    event = {"channel": "C123", "text": "trigger", "user": "U2", "ts": "9"}
    await app.events["message"](event, SimpleNamespace())
    adapter.handle_inbound_chat_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_event_message_without_storage_is_noop(monkeypatch):
    adapter = _mock_adapter(sp=False)
    _, app = _install(monkeypatch, _FakeEntry({"C123": adapter}))
    event = {"channel": "C123", "text": "hi", "user": "U", "ts": "1"}
    await app.events["message"](event, SimpleNamespace())
    adapter.handle_inbound_chat_message.assert_not_awaited()


# --------------------------------------------------------------------------- #
# view: agent-switch modal submit
# --------------------------------------------------------------------------- #
def _switch_view(chat="c1", agent="a1", meta="C123") -> dict:
    return {
        "private_metadata": meta,
        "state": {"values": {
            "chat_b": {"chat_s": {"selected_option": {"value": chat}}},
            "agent_b": {"agent_s": {"selected_option": {"value": agent}}},
        }},
    }


class _ExecSetAgent:
    def __init__(self, *, storage_provider):
        pass

    async def set_agent(self, *, chat_id, agent_id, channel_id):
        return SimpleNamespace(text=f"Switched to {agent_id}.")


@pytest.mark.asyncio
async def test_view_agent_switch_confirms_in_thread(monkeypatch):
    chat = SimpleNamespace(
        channel_binding=SimpleNamespace(thread_external_id="TT"))
    storage = SimpleNamespace(get=AsyncMock(return_value=chat))
    adapter = SimpleNamespace(
        _sp=SimpleNamespace(get_storage=lambda model: storage),
        _channel=_channel())
    _, app = _install(monkeypatch, _FakeEntry({"C123": adapter}))
    monkeypatch.setattr(
        "primer.channel.commands.CommandExecutor", _ExecSetAgent)
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    handler = app.views[slack_factory.AGENT_SWITCH_MODAL_CALLBACK_ID]
    await handler(AsyncMock(), {}, _switch_view(agent="a1"), client)
    client.chat_postMessage.assert_awaited_once()
    kw = client.chat_postMessage.await_args.kwargs
    assert kw["thread_ts"] == "TT"
    assert kw["text"] == "Switched to a1."


@pytest.mark.asyncio
async def test_view_agent_switch_info_only_is_noop(monkeypatch):
    adapter = _mock_adapter()
    _, app = _install(monkeypatch, _FakeEntry({"C123": adapter}))
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    handler = app.views[slack_factory.AGENT_SWITCH_MODAL_CALLBACK_ID]
    # No "state" -> read_agent_switch_submission returns None.
    await handler(AsyncMock(), {}, {"private_metadata": "C123"}, client)
    client.chat_postMessage.assert_not_awaited()


@pytest.mark.asyncio
async def test_view_agent_switch_unknown_channel_is_noop(monkeypatch):
    _, app = _install(monkeypatch, _FakeEntry({}))
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    handler = app.views[slack_factory.AGENT_SWITCH_MODAL_CALLBACK_ID]
    await handler(AsyncMock(), {}, _switch_view(meta="MISSING"), client)
    client.chat_postMessage.assert_not_awaited()


@pytest.mark.asyncio
async def test_view_agent_switch_set_agent_error_swallowed(monkeypatch):
    adapter = SimpleNamespace(
        _sp=SimpleNamespace(get_storage=lambda m: None), _channel=_channel())
    _, app = _install(monkeypatch, _FakeEntry({"C123": adapter}))

    class _Boom:
        def __init__(self, *, storage_provider):
            pass

        async def set_agent(self, **kw):
            raise RuntimeError("nope")

    monkeypatch.setattr("primer.channel.commands.CommandExecutor", _Boom)
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    handler = app.views[slack_factory.AGENT_SWITCH_MODAL_CALLBACK_ID]
    await handler(AsyncMock(), {}, _switch_view(), client)
    client.chat_postMessage.assert_not_awaited()


# --------------------------------------------------------------------------- #
# idempotent handler installation + exception swallowing
# --------------------------------------------------------------------------- #
def test_install_handlers_is_idempotent(monkeypatch):
    pid = _uid()
    monkeypatch.setattr(
        slack_factory, "SLACK_CONNECTIONS", _FakeRegistry(_FakeEntry()))
    app1 = _FakeApp()
    slack_factory._install_handlers(pid, app1)
    assert app1.actions  # first install populated the app
    app2 = _FakeApp()
    slack_factory._install_handlers(pid, app2)  # same pid -> early return
    assert app2.actions == {}


@pytest.mark.asyncio
async def test_action_approve_chat_update_error_swallowed(monkeypatch):
    adapter = _mock_adapter()
    _, app = _install(monkeypatch, _FakeEntry({"C123": adapter}))
    client = SimpleNamespace(chat_update=AsyncMock(side_effect=RuntimeError("x")))
    body = {
        "actions": [{"value": "approve:w:s:t"}],
        "channel": {"id": "C123"}, "user": {"id": "U"},
        "message": {"ts": "1", "blocks": []},
    }
    # Must not raise even though chat.update fails; decision still recorded.
    await app.actions["approve"](AsyncMock(), body, client)
    adapter._handle_decision.assert_awaited_once()


@pytest.mark.asyncio
async def test_event_message_correlation_lookup_error_falls_through(monkeypatch):
    adapter = _mock_adapter()
    _, app = _install(monkeypatch, _FakeEntry({"C123": adapter}))

    class Corr:
        def __init__(self, sp):
            pass

        async def lookup(self, cid, key):
            raise RuntimeError("store down")

    monkeypatch.setattr("primer.channel.correlation.CorrelationStore", Corr)
    monkeypatch.setattr(
        slack_factory, "_route_channel_event", AsyncMock(return_value=False))
    event = {"channel": "C123", "thread_ts": "TT",
             "text": "hi", "user": "U", "ts": "2"}
    await app.events["message"](event, SimpleNamespace())
    # lookup failed -> rec None -> chat-surface dispatch owns delivery.
    adapter.handle_inbound_chat_message.assert_awaited_once()


# --------------------------------------------------------------------------- #
# _slack_factory
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_slack_factory_builds_adapter_and_installs_handlers(monkeypatch):
    created: dict = {}

    class _FakeAdapter:
        def __init__(self, **kw):
            created.update(kw)
            self.initialized = False

        async def initialize(self):
            self.initialized = True

    installed: list = []
    monkeypatch.setattr(slack_factory, "SlackChannelAdapter", _FakeAdapter)
    monkeypatch.setattr(
        slack_factory, "_install_handlers",
        lambda pid, app: installed.append((pid, app)))
    app_obj = object()
    entry = SimpleNamespace(conn=SimpleNamespace(app=app_obj))
    monkeypatch.setattr(slack_factory, "SLACK_CONNECTIONS", _FakeRegistry(entry))

    provider = _provider("cp-x")
    channel = _channel()
    inbox = object()
    adapter = await slack_factory._slack_factory(
        provider, channel, inbox,
        storage_provider="SP", event_bus="EB",
        claim_engine="CE", artifact_registry="AR")

    assert isinstance(adapter, _FakeAdapter)
    assert adapter.initialized is True
    assert created["provider"] is provider
    assert created["channel"] is channel
    assert created["inbox"] is inbox
    assert created["storage_provider"] == "SP"
    assert created["event_bus"] == "EB"
    assert created["claim_engine"] == "CE"
    assert created["artifact_registry"] == "AR"
    assert installed == [(provider.id, app_obj)]


@pytest.mark.asyncio
async def test_slack_factory_without_connection_skips_handlers(monkeypatch):
    class _FakeAdapter:
        def __init__(self, **kw):
            pass

        async def initialize(self):
            pass

    installed: list = []
    monkeypatch.setattr(slack_factory, "SlackChannelAdapter", _FakeAdapter)
    monkeypatch.setattr(
        slack_factory, "_install_handlers",
        lambda pid, app: installed.append(pid))
    monkeypatch.setattr(slack_factory, "SLACK_CONNECTIONS", _FakeRegistry(None))

    adapter = await slack_factory._slack_factory(
        _provider("cp-y"), _channel(), object())
    assert isinstance(adapter, _FakeAdapter)
    assert installed == []
