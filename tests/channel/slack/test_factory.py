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
        # The mentions_bot fallback path; unknown id by default (the
        # tests that exercise the flag set it explicitly).
        bot_user_id=AsyncMock(return_value=None),
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
    adapter = SimpleNamespace(_inbound_router=lambda: None, _channel=_channel())
    assert await slack_factory._route_channel_event(adapter, "p", {}) is False


@pytest.mark.asyncio
async def test_route_channel_event_normalized_none(monkeypatch):
    router = SimpleNamespace(
        has_matching_rule=AsyncMock(return_value=True),
        route_event=AsyncMock())
    adapter = SimpleNamespace(
        _inbound_router=lambda: router, _channel=_channel(),
        collect_inbound_media=AsyncMock(return_value=[]),
    )
    _patch_normalizer(monkeypatch, None)
    assert await slack_factory._route_channel_event(adapter, "p", {}) is False
    router.route_event.assert_not_awaited()




@pytest.mark.asyncio
async def test_route_channel_event_match_dispatches(monkeypatch):
    router = SimpleNamespace(
        has_matching_rule=AsyncMock(return_value=True),
        route_event=AsyncMock())
    adapter = SimpleNamespace(
        _inbound_router=lambda: router, _channel=_channel(),
        collect_inbound_media=AsyncMock(return_value=[]),
    )
    _patch_normalizer(monkeypatch, {"norm": True})
    assert await slack_factory._route_channel_event(adapter, "p", {}) is True
    router.route_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_route_channel_event_swallows_exception(monkeypatch):
    router = SimpleNamespace(
        route_event=AsyncMock(side_effect=RuntimeError("boom")))
    adapter = SimpleNamespace(
        _inbound_router=lambda: router, _channel=_channel(),
        collect_inbound_media=AsyncMock(return_value=[]),
    )
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


















# --------------------------------------------------------------------------- #
# event: message
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_event_message_ignores_bot(monkeypatch):
    adapter = _mock_adapter()
    _, app = _install(monkeypatch, _FakeEntry({"C123": adapter}))
    await app.events["message"]({"bot_id": "B1"}, SimpleNamespace(), SimpleNamespace())
    adapter.handle_inbound_chat_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_event_message_ignores_edit_subtype(monkeypatch):
    adapter = _mock_adapter()
    _, app = _install(monkeypatch, _FakeEntry({"C123": adapter}))
    await app.events["message"](
        {"subtype": "message_changed", "channel": "C123"}, SimpleNamespace(),
        SimpleNamespace())
    adapter.handle_inbound_chat_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_event_message_unknown_channel_is_noop(monkeypatch):
    adapter = _mock_adapter()
    _, app = _install(monkeypatch, _FakeEntry({"C123": adapter}))
    await app.events["message"](
        {"channel": "OTHER", "ts": "1"}, SimpleNamespace(), SimpleNamespace())
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

        async def clear_gate(self, cid, key):
            cleared.append(key)

        async def clear(self, cid, key):
            raise AssertionError("an answer must not delete the thread's mapping")

    monkeypatch.setattr("primer.channel.correlation.CorrelationStore", Corr)
    event = {"channel": "C123", "thread_ts": "TT",
             "text": "answer", "user": "U", "ts": "1"}
    await app.events["message"](event, SimpleNamespace(), SimpleNamespace())
    adapter._handle_text_reply.assert_awaited_once()
    assert adapter._handle_text_reply.await_args.kwargs["text"] == "answer"
    assert cleared == ["TT"], "the answered question was not cleared from the thread"
    adapter.handle_inbound_chat_message.assert_not_awaited()






@pytest.mark.asyncio
async def test_event_message_without_storage_is_noop(monkeypatch):
    adapter = _mock_adapter(sp=False)
    _, app = _install(monkeypatch, _FakeEntry({"C123": adapter}))
    event = {"channel": "C123", "text": "hi", "user": "U", "ts": "1"}
    await app.events["message"](event, SimpleNamespace(), SimpleNamespace())
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
    await app.events["message"](event, SimpleNamespace(), SimpleNamespace())
    # lookup failed -> rec None -> the message still routes as an event
    # (S6 section 5: there is no chat dispatch to fall through to).
    slack_factory._route_channel_event.assert_awaited_once()


# --------------------------------------------------------------------------- #
# event: message - mentions_bot (the bot user named as <@BOT_USER_ID>)
# --------------------------------------------------------------------------- #
class _EventCaptureRouter:
    """Captures every normalized event handed to route_event."""

    def __init__(self) -> None:
        self.events: list = []

    async def route_event(self, *, event, channel, media_parts=None):
        self.events.append(event)


def _bot_context(bot_user_id: str | None = "UBOT") -> SimpleNamespace:
    ctx = SimpleNamespace()
    if bot_user_id is not None:
        ctx.bot_user_id = bot_user_id
    return ctx


def _routing_adapter(router) -> SimpleNamespace:
    adapter = _mock_adapter()
    adapter._inbound_router = lambda: router
    adapter.collect_inbound_media = AsyncMock(return_value=[])
    return adapter


@pytest.mark.asyncio
async def test_message_that_names_the_bot_user_sets_mentions_bot(monkeypatch):
    router = _EventCaptureRouter()
    adapter = _routing_adapter(router)
    _, app = _install(monkeypatch, _FakeEntry({"C123": adapter}))
    event = {"channel": "C123", "text": "<@UBOT> hi", "user": "U", "ts": "1"}
    await app.events["message"](event, SimpleNamespace(), _bot_context("UBOT"))
    assert len(router.events) == 1
    assert router.events[0].mentions_bot is True


@pytest.mark.asyncio
async def test_message_that_names_only_another_user_does_not_set_it(monkeypatch):
    router = _EventCaptureRouter()
    adapter = _routing_adapter(router)
    _, app = _install(monkeypatch, _FakeEntry({"C123": adapter}))
    event = {"channel": "C123", "text": "<@UOTHER> hi", "user": "U", "ts": "1"}
    await app.events["message"](event, SimpleNamespace(), _bot_context("UBOT"))
    assert len(router.events) == 1
    assert router.events[0].mentions_bot is False


@pytest.mark.asyncio
async def test_message_without_context_bot_id_uses_the_adapter_cache(monkeypatch):
    # The bolt context has no bot_user_id (e.g. a token it could not
    # resolve): the adapter's own cached auth.test id is the fallback.
    router = _EventCaptureRouter()
    adapter = _routing_adapter(router)
    adapter.bot_user_id = AsyncMock(return_value="UBOT")
    _, app = _install(monkeypatch, _FakeEntry({"C123": adapter}))
    event = {"channel": "C123", "text": "<@UBOT> hi", "user": "U", "ts": "1"}
    await app.events["message"](event, SimpleNamespace(), _bot_context(None))
    assert len(router.events) == 1
    assert router.events[0].mentions_bot is True
    adapter.bot_user_id.assert_awaited_once()


@pytest.mark.asyncio
async def test_message_with_unknown_bot_id_is_not_a_mention(monkeypatch):
    # Missing identity (no context value, no cached auth.test): the flag is
    # False, never an error.
    router = _EventCaptureRouter()
    adapter = _routing_adapter(router)  # bot_user_id defaults to None
    _, app = _install(monkeypatch, _FakeEntry({"C123": adapter}))
    event = {"channel": "C123", "text": "hi", "user": "U", "ts": "1"}
    await app.events["message"](event, SimpleNamespace(), _bot_context(None))
    assert len(router.events) == 1
    assert router.events[0].mentions_bot is False


@pytest.mark.asyncio
async def test_only_the_message_event_is_routed_so_a_mention_routes_once(
    monkeypatch,
):
    # Slack sends an app_mention event AND the message event for one
    # mention. Only the message event is routed, so the mention must not
    # be delivered twice.
    router = _EventCaptureRouter()
    adapter = _routing_adapter(router)
    _, app = _install(monkeypatch, _FakeEntry({"C123": adapter}))
    assert "message" in app.events
    assert "app_mention" not in app.events
    event = {"channel": "C123", "text": "<@UBOT> hi", "user": "U", "ts": "1"}
    await app.events["message"](event, SimpleNamespace(), _bot_context("UBOT"))
    assert len(router.events) == 1


# --------------------------------------------------------------------------- #
# End-to-end: a binding on mentions_bot fires for the mentioning message only
# --------------------------------------------------------------------------- #
#
# Drives the real ChannelEventRouter + the real fire_trigger over the
# top-level fake_storage_provider, through the real _on_message handler:
# the bot id comes from the bolt context, the flag is computed from the
# message text, the real Slack normalizer stamps it, and the session_append
# steer fixture of tests/trigger/test_dispatch_event_matcher.py (a queued
# steer behind an open turn, whose observable is the PendingSessionMessage
# row) says whether the binding matched.
class _FakeScheduler:
    def __init__(self) -> None:
        self.enqueued: list[str] = []

    async def enqueue(self, sid: str) -> None:
        self.enqueued.append(sid)


class _FakeClaimEngine:
    def __init__(self) -> None:
        self.upserts: list = []

    async def upsert(self, kind, entity_id, *, priority=100, next_attempt_at=None):
        self.upserts.append((kind, entity_id, priority))


class _FakeBus:
    def __init__(self) -> None:
        self.published: list = []

    async def publish(self, event_key, payload=None):
        self.published.append((event_key, payload or {}))


@pytest.mark.asyncio
async def test_a_slack_binding_on_mentions_bot_fires_only_for_the_mentioning_message(
    monkeypatch, fake_storage_provider,
):
    from datetime import datetime, timezone

    from primer.channel.correlation import CorrelationStore
    from primer.channel.event_dispatch import ChannelEventRouter
    from primer.model.agent import Agent, AgentModel
    from primer.model.channel_event import NormalizedEventType
    from primer.model.event_matcher import EventMatcher
    from primer.model.trigger import (
        ChannelTriggerConfig,
        SessionAppendSubConfig,
        Subscription,
        Trigger,
    )
    from primer.model.workspace_session import (
        AgentSessionBinding,
        PendingSessionMessage,
        SessionStatus,
        WorkspaceSession,
    )
    from primer.trigger.subscribers import DispatchDeps

    sp = fake_storage_provider
    now = datetime.now(timezone.utc)

    adapter = _mock_adapter()
    adapter._inbound_router = lambda: None  # set below, after the router
    adapter.collect_inbound_media = AsyncMock(return_value=[])
    adapter.bot_user_id = AsyncMock(return_value=None)  # context path only
    entry = _FakeEntry({"C123": adapter})
    pid, app = _install(monkeypatch, entry)

    ch = _channel()
    await sp.get_storage(Channel).create(ch)
    await sp.get_storage(Agent).create(Agent(
        id="ag-1", description="seeded test agent",
        model=AgentModel(profile_id="p--m"),
    ))
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id="cn-1", workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="ag-1"),
        status=SessionStatus.RUNNING, turn_status="running",
        created_at=now,
    ))
    await sp.get_storage(Trigger).create(Trigger(
        id="trg-s1", slug="mentions-bot", name="mentions binding",
        config=ChannelTriggerConfig(provider_id=pid, channel_id=ch.id),
        created_at=now,
    ))
    await sp.get_storage(Subscription).create(Subscription(
        id="sb-mentions", trigger_id="trg-s1",
        config=SessionAppendSubConfig(session_id="cn-1"),
        payload_template="hi",
        parallelism="queue",
        event_matcher=EventMatcher(
            event_type=NormalizedEventType.MESSAGE_POSTED, mentions_bot=True,
        ),
        enabled=True,
        created_at=now,
    ))

    deps = DispatchDeps(
        storage_provider=sp, claim_engine=_FakeClaimEngine(),
        scheduler=_FakeScheduler(), workspace_registry=object(),
    )
    router = ChannelEventRouter(
        storage_provider=sp, correlation_store=CorrelationStore(sp),
        fire_deps=deps, event_bus=_FakeBus(),
    )
    adapter._inbound_router = lambda: router

    pending = sp.get_storage(PendingSessionMessage)._data

    # The message that names the bot user: the binding matches, the steer
    # is queued behind the open turn.
    event = {"channel": "C123", "text": "<@UBOT> hi", "user": "U", "ts": "1"}
    await app.events["message"](event, SimpleNamespace(), _bot_context("UBOT"))
    assert len(pending) == 1
    row = next(iter(pending.values()))
    assert row.session_id == "cn-1"
    assert row.parts == [{"type": "text", "text": "hi"}]

    # The message that names someone else: the matcher refuses, nothing
    # new is queued.
    event = {"channel": "C123", "text": "<@UOTHER> hi", "user": "U", "ts": "2"}
    await app.events["message"](event, SimpleNamespace(), _bot_context("UBOT"))
    assert len(pending) == 1


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


# --------------------------------------------------------------------------- #
# a refused approval is reported to the clicker (ticket 01a11b6c)
# --------------------------------------------------------------------------- #
NOTICE = "routed to specific approvers"


@pytest.mark.asyncio
async def test_action_approve_refused_tells_the_clicker_and_does_not_mark_the_message_decided(monkeypatch):
    """The relay raised on a restricted gate, the decided edit was skipped, and the clicker saw nothing."""
    adapter = _mock_adapter()
    adapter._handle_decision = AsyncMock(return_value=False)
    _, app = _install(monkeypatch, _FakeEntry({"C123": adapter}))
    client = SimpleNamespace(chat_update=AsyncMock(), chat_postEphemeral=AsyncMock())
    body = {
        "actions": [{"value": "approve:ws1:sid1:tc1"}],
        "channel": {"id": "C123"}, "user": {"id": "U9"},
        "message": {"ts": "111.222", "blocks": [{"type": "actions"}]},
    }

    await app.actions["approve"](AsyncMock(), body, client)

    client.chat_update.assert_not_awaited()
    client.chat_postEphemeral.assert_awaited_once()
    kw = client.chat_postEphemeral.await_args.kwargs
    assert kw["channel"] == "C123" and kw["user"] == "U9" and NOTICE in kw["text"]


@pytest.mark.asyncio
async def test_action_approve_accepted_is_not_told_anything_extra(monkeypatch):
    adapter = _mock_adapter()
    adapter._handle_decision = AsyncMock(return_value=True)
    _, app = _install(monkeypatch, _FakeEntry({"C123": adapter}))
    client = SimpleNamespace(chat_update=AsyncMock(), chat_postEphemeral=AsyncMock())
    body = {
        "actions": [{"value": "approve:ws1:sid1:tc1"}],
        "channel": {"id": "C123"}, "user": {"id": "U9"},
        "message": {"ts": "111.222", "blocks": [{"type": "actions"}]},
    }

    await app.actions["approve"](AsyncMock(), body, client)

    client.chat_update.assert_awaited_once()
    client.chat_postEphemeral.assert_not_awaited()


@pytest.mark.asyncio
async def test_view_reject_modal_refused_tells_the_clicker_and_leaves_the_message(monkeypatch):
    adapter = _mock_adapter()
    adapter._handle_decision = AsyncMock(return_value=False)
    _, app = _install(monkeypatch, _FakeEntry({"C123": adapter}))
    client = SimpleNamespace(
        conversations_history=AsyncMock(return_value={"messages": [{"blocks": []}]}),
        chat_update=AsyncMock(), chat_postEphemeral=AsyncMock())
    view = {
        "private_metadata": "reject:ws:sid:tc:C123:111.2",
        "state": {"values": {"reason": {"reason_text": {"value": "no good"}}}},
    }

    await app.views[slack_factory.REJECT_MODAL_CALLBACK_ID](AsyncMock(), {"user": {"id": "U1"}}, view, client)

    client.chat_update.assert_not_awaited()
    client.chat_postEphemeral.assert_awaited_once()
    kw = client.chat_postEphemeral.await_args.kwargs
    assert kw["channel"] == "C123" and kw["user"] == "U1" and NOTICE in kw["text"]


@pytest.mark.asyncio
async def test_a_failing_notice_does_not_break_the_handler(monkeypatch):
    adapter = _mock_adapter()
    adapter._handle_decision = AsyncMock(return_value=False)
    _, app = _install(monkeypatch, _FakeEntry({"C123": adapter}))
    client = SimpleNamespace(chat_update=AsyncMock(), chat_postEphemeral=AsyncMock(side_effect=RuntimeError("slack down")))
    body = {
        "actions": [{"value": "approve:ws1:sid1:tc1"}],
        "channel": {"id": "C123"}, "user": {"id": "U9"}, "message": {"ts": "1.2", "blocks": []},
    }

    await app.actions["approve"](AsyncMock(), body, client)          # must not raise

    client.chat_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_view_reject_modal_with_no_adapter_to_relay_through_edits_nothing(monkeypatch):
    """With no adapter under the provider nothing was relayed: the message must not be edited to say it was rejected."""
    _, app = _install(monkeypatch, _FakeEntry({}))
    client = SimpleNamespace(
        conversations_history=AsyncMock(return_value={"messages": [{"blocks": []}]}),
        chat_update=AsyncMock(), chat_postEphemeral=AsyncMock())
    view = {
        "private_metadata": "reject:ws:sid:tc:C123:111.2",
        "state": {"values": {"reason": {"reason_text": {"value": "no good"}}}},
    }

    await app.views[slack_factory.REJECT_MODAL_CALLBACK_ID](AsyncMock(), {"user": {"id": "U1"}}, view, client)

    client.chat_update.assert_not_awaited()
    client.chat_postEphemeral.assert_not_awaited()

