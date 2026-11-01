"""Channel connections (Part 2 Task 73: W7, W8, W37, W38).

* W8: an adapter held its provider's shared connection twice (the factory and the registry both called ``initialize()``),
  so ``invalidate`` never closed it and a rotated bot token was ignored until a restart; and once closed, a reopened
  connection got no handlers because the provider id stayed marked as installed.
* W7: a worker-only process opened inbound gateways (a Telegram poller, a Discord gateway, a Slack socket) the first
  time it posted. A registry that does not own inbound takes outbound-only connections and installs no handlers.
* W37: Telegram processed every update of every chat one at a time.
* W38: a 429 was not retried on Slack or Telegram.

Fakes only: no live Slack, Discord or Telegram.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from primer.api.registries.channel_registry import ChannelRegistry
from primer.channel.factory import register_adapter_factory
from primer.channel.inbox import ChannelInbox
from primer.model.channel import (
    Channel, ChannelProvider, ChannelProviderType, SlackChannelProviderConfig, TelegramChannelProviderConfig,
)
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider

_TG_TOKEN = "123456:abcdefghijklmnopqrstuvwxyz123456"


# ---- fakes -----------------------------------------------------------------------------------------------------------


class _BoltApp:
    """A slack_bolt AsyncApp's handler decorators, recording what was registered."""

    def __init__(self, token: str) -> None:
        self.token = token
        self.handlers: list[tuple[str, tuple]] = []
        self.client = SimpleNamespace(token=token)

    def _record(self, kind):
        def deco(*a, **_k):
            return lambda f: (self.handlers.append((kind, a)), f)[1]
        return deco

    def __getattr__(self, name):
        if name in ("action", "view", "event"):
            return self._record(name)
        raise AttributeError(name)


@pytest.fixture
def slack(monkeypatch):
    """The Slack connection module with fake builders: records every app built, socket connected and connection closed."""
    import primer.channel.slack.connection as conn_mod
    import primer.channel.slack.factory as factory_mod

    register_adapter_factory(ChannelProviderType.SLACK, factory_mod._slack_factory)  # another test may have cleared it
    monkeypatch.setattr(factory_mod, "_HANDLERS_INSTALLED", set())
    monkeypatch.setattr(conn_mod.SLACK_CONNECTIONS, "_entries", {})
    rec = SimpleNamespace(apps=[], connected=[], closed=[])

    def _conn(cfg, *, socket: bool):
        token = cfg.bot_token.get_secret_value()
        app = _BoltApp(token)
        rec.apps.append(app)

        class _C:
            def __init__(self):
                self.app = app

            async def start_async(self):
                if socket:
                    rec.connected.append(token)

            async def close_async(self):
                rec.closed.append(token)
        return _C()

    monkeypatch.setattr(conn_mod, "_build_app_and_handler", lambda cfg: _conn(cfg, socket=True))
    monkeypatch.setattr(conn_mod, "_build_outbound_app", lambda cfg: _conn(cfg, socket=False))
    rec.conn_mod, rec.factory_mod = conn_mod, factory_mod
    return rec


def _slack_provider(pid: str = "cp-1", token: str = "xoxb-OLD-aaaaaaaaaaaa") -> ChannelProvider:
    return ChannelProvider(id=pid, provider=ChannelProviderType.SLACK, config=SlackChannelProviderConfig(
        app_token=SecretStr("xapp-1-aaaaaaaaaaaaaaaa"), bot_token=SecretStr(token)))


async def _sqlite(tmp_path: Path) -> SqliteStorageProvider:
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "r.sqlite"))
    await p.initialize()
    return p


def _registry(p, **kw) -> ChannelRegistry:
    return ChannelRegistry(
        channel_storage=p.get_storage(Channel), channel_provider_storage=p.get_storage(ChannelProvider),
        inbox=ChannelInbox(event_bus=None), storage_provider=p, **kw,
    )


class _TgApp:
    """A PTB Application: records the lifecycle calls, the handlers added and the messages sent."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.handlers: list = []
        self.sent: list[dict] = []
        app = self

        class _Updater:
            async def start_polling(self, **_k):
                app.calls.append("start_polling")

            async def stop(self):
                app.calls.append("updater.stop")

        class _Bot:
            async def send_message(self, **body):
                app.sent.append(body)
                return SimpleNamespace(message_id=40 + len(app.sent))

        self.updater, self.bot = _Updater(), _Bot()

    async def initialize(self):
        self.calls.append("initialize")

    async def start(self):
        self.calls.append("start")

    async def stop(self):
        self.calls.append("stop")

    async def shutdown(self):
        self.calls.append("shutdown")

    def add_handler(self, handler):
        self.handlers.append(handler)


@pytest.fixture
def telegram(monkeypatch):
    import primer.channel.telegram.connection as conn_mod
    import primer.channel.telegram.factory as factory_mod

    register_adapter_factory(ChannelProviderType.TELEGRAM, factory_mod._telegram_factory)
    monkeypatch.setattr(factory_mod, "_HANDLERS_INSTALLED", set())
    monkeypatch.setattr(conn_mod.TELEGRAM_CONNECTIONS, "_entries", {})
    apps: list[_TgApp] = []

    def _build(cfg):
        apps.append(_TgApp())
        return apps[-1]

    monkeypatch.setattr(conn_mod, "_build_application", _build)
    return apps


def _tg_provider(pid: str = "cp-tg") -> ChannelProvider:
    return ChannelProvider(id=pid, provider=ChannelProviderType.TELEGRAM,
                           config=TelegramChannelProviderConfig(bot_token=SecretStr(_TG_TOKEN)))


# ---- characterization: one process owning inbound, as today ----------------------------------------------------------


async def test_an_inbound_telegram_connection_initializes_starts_and_polls_then_shuts_down(telegram):
    from primer.channel.telegram.connection import TELEGRAM_CONNECTIONS

    await TELEGRAM_CONNECTIONS.acquire(_tg_provider())
    assert telegram[0].calls == ["initialize", "start", "start_polling"]
    await TELEGRAM_CONNECTIONS.release(_tg_provider())
    assert telegram[0].calls[3:] == ["updater.stop", "stop", "shutdown"]


async def test_the_slack_factory_installs_handlers_once_per_connection(slack):
    ch1 = Channel(id="ch-1", provider_id="cp-1", provider=ChannelProviderType.SLACK, external_id="C1")
    ch2 = Channel(id="ch-2", provider_id="cp-1", provider=ChannelProviderType.SLACK, external_id="C2")
    a = await slack.factory_mod._slack_factory(_slack_provider(), ch1, None)
    b = await slack.factory_mod._slack_factory(_slack_provider(), ch2, None)
    assert len(slack.apps) == 1 and slack.connected == ["xoxb-OLD-aaaaaaaaaaaa"]
    assert [kind for kind, _ in slack.apps[0].handlers] == ["action", "action", "view", "event"]
    entry = slack.conn_mod.SLACK_CONNECTIONS.entry("cp-1")
    assert entry.adapters_by_channel_id == {"C1": a, "C2": b}
    await a.aclose()
    await b.aclose()


async def test_the_registry_caches_one_adapter_per_channel_and_invalidate_closes_it(tmp_path, slack):
    p = await _sqlite(tmp_path)
    try:
        await p.get_storage(ChannelProvider).create(_slack_provider())
        await p.get_storage(Channel).create(Channel(id="ch-1", provider_id="cp-1", provider=ChannelProviderType.SLACK, external_id="C1"))
        reg = _registry(p)
        a = await reg.get_adapter("ch-1")
        assert await reg.get_adapter("ch-1") is a and reg.peek_adapter("ch-1") is a
        await reg.invalidate(channel_id="ch-1")
        assert reg.peek_adapter("ch-1") is None
        assert slack.conn_mod.SLACK_CONNECTIONS.entry("cp-1") is None or "C1" not in (
            slack.conn_mod.SLACK_CONNECTIONS.entry("cp-1").adapters_by_channel_id
        )
        await reg.aclose()
    finally:
        await p.aclose()


def _update(chat_id: int):
    return SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id))


async def test_telegram_updates_of_one_chat_are_processed_in_arrival_order():
    from primer.channel.telegram.connection import _build_application

    processor = _build_application(TelegramChannelProviderConfig(bot_token=SecretStr(_TG_TOKEN))).update_processor
    log: list[str] = []

    async def _handle(name: str):
        log.append(f"{name}:start")
        await asyncio.sleep(0.01)
        log.append(f"{name}:end")

    await asyncio.wait_for(asyncio.gather(*[
        processor.process_update(_update(1), _handle(f"a{i}")) for i in range(3)
    ]), timeout=5)
    assert log == ["a0:start", "a0:end", "a1:start", "a1:end", "a2:start", "a2:end"]


# ---- W8: one acquire per adapter; a reopened connection gets its handlers ---------------------------------------------


async def test_W8_an_adapter_holds_its_connection_once_and_invalidate_reopens_it_with_the_rotated_token(tmp_path, slack):
    p = await _sqlite(tmp_path)
    try:
        await p.get_storage(ChannelProvider).create(_slack_provider())
        await p.get_storage(Channel).create(Channel(id="ch-1", provider_id="cp-1", provider=ChannelProviderType.SLACK, external_id="C1"))
        reg = _registry(p)
        await reg.get_adapter("ch-1")
        assert slack.conn_mod.SLACK_CONNECTIONS.entry("cp-1").refcount == 1

        # The operator rotates the bot token; the provider hook flushes the adapters.
        row = await p.get_storage(ChannelProvider).get("cp-1")
        await p.get_storage(ChannelProvider).update(row.model_copy(update={"config": SlackChannelProviderConfig(
            app_token=SecretStr("xapp-1-aaaaaaaaaaaaaaaa"), bot_token=SecretStr("xoxb-NEW-aaaaaaaaaaaa"))}))
        await reg.invalidate(channel_id=None)
        assert slack.closed == ["xoxb-OLD-aaaaaaaaaaaa"]
        assert slack.conn_mod.SLACK_CONNECTIONS.entry("cp-1") is None

        await reg.get_adapter("ch-1")
        assert slack.connected == ["xoxb-OLD-aaaaaaaaaaaa", "xoxb-NEW-aaaaaaaaaaaa"]
        await reg.aclose()
        assert slack.conn_mod.SLACK_CONNECTIONS.entry("cp-1") is None
    finally:
        await p.aclose()


async def test_W8_a_reopened_slack_connection_gets_its_handlers_again(slack):
    ch = Channel(id="ch-2", provider_id="cp-2", provider=ChannelProviderType.SLACK, external_id="C2")
    a = await slack.factory_mod._slack_factory(_slack_provider("cp-2"), ch, None)
    await a.aclose()  # refcount 1 -> 0: the connection is closed
    b = await slack.factory_mod._slack_factory(_slack_provider("cp-2"), ch, None)
    assert len(slack.apps) == 2
    assert slack.apps[1].handlers == slack.apps[0].handlers and slack.apps[1].handlers
    await b.aclose()


async def test_W8_a_reopened_telegram_connection_gets_its_handlers_again(telegram):
    from primer.channel.telegram import factory as tg_factory

    ch = Channel(id="ch-1", provider_id="cp-tg", provider=ChannelProviderType.TELEGRAM, external_id="100")
    a = await tg_factory._telegram_factory(_tg_provider(), ch, None)
    await a.aclose()
    b = await tg_factory._telegram_factory(_tg_provider(), ch, None)
    assert len(telegram) == 2 and len(telegram[1].handlers) == len(telegram[0].handlers) == 2
    await b.aclose()


async def test_W8_a_reopened_discord_connection_gets_its_handlers_again(monkeypatch):
    from primer.channel.discord import connection as dc_conn
    from primer.channel.discord import factory as dc_factory
    from primer.model.channel import DiscordChannelProviderConfig

    monkeypatch.setattr(dc_factory, "_HANDLERS_INSTALLED", set())
    monkeypatch.setattr(dc_conn.DISCORD_CONNECTIONS, "_entries", {})
    clients: list[SimpleNamespace] = []

    def _build(cfg):
        clients.append(SimpleNamespace(on_message=None, on_interaction=None, close=_noop))
        return clients[-1]

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(dc_conn, "_build_client", _build)
    monkeypatch.setattr(dc_conn, "_start_client_as_task", _noop)
    provider = ChannelProvider(id="cp-dc", provider=ChannelProviderType.DISCORD,
                               config=DiscordChannelProviderConfig(bot_token=SecretStr("x" * 60)))
    ch = Channel(id="ch-1", provider_id="cp-dc", provider=ChannelProviderType.DISCORD, external_id="500")
    a = await dc_factory._discord_factory(provider, ch, None)
    await a.aclose()
    b = await dc_factory._discord_factory(provider, ch, None)
    assert len(clients) == 2 and clients[1].on_message is not None and clients[1].on_interaction is not None
    await b.aclose()


# ---- W7: a registry that does not own inbound opens no poller, gateway or socket ---------------------------------------


async def test_W7_a_worker_only_registry_dispatches_a_prompt_without_polling_or_handlers(tmp_path, telegram):
    from primer.channel.adapter import PromptEnvelope
    from primer.channel.dispatcher import ChannelDispatcher
    from primer.model.workspace import Workspace, WorkspaceChannelLink, WorkspaceRuntimeMeta

    p = await _sqlite(tmp_path)
    try:
        await p.get_storage(ChannelProvider).create(_tg_provider())
        await p.get_storage(Channel).create(Channel(id="ch-1", provider_id="cp-tg", provider=ChannelProviderType.TELEGRAM, external_id="100"))
        await p.get_storage(Workspace).create(Workspace(
            id="ws-1", template_id="t-1", provider_id="p-1", created_at=datetime.now(UTC),
            runtime_meta=WorkspaceRuntimeMeta(url="ws://localhost:5959", token=SecretStr("t")),
            reply_binding=WorkspaceChannelLink(channel_id="ch-1"),
        ))
        reg = _registry(p, owns_inbound=False)
        results = await ChannelDispatcher(registry=reg).dispatch_prompt(envelope=PromptEnvelope(
            kind="inform", workspace_id="ws-1", session_id=None, tool_call_id=None, prompt="done",
            response_schema=None, choices=None, timeout_at_iso=None,
        ))
        assert results and "error" not in results[0]
        assert [m["text"] for m in telegram[0].sent] == ["done"]
        assert telegram[0].calls == ["initialize"]          # no start(), no start_polling()
        assert telegram[0].handlers == []
        await reg.aclose()
        assert telegram[0].calls == ["initialize", "shutdown"]
    finally:
        await p.aclose()


async def test_W7_an_outbound_only_slack_connection_opens_no_socket_and_installs_no_handlers(slack):
    ch = Channel(id="ch-1", provider_id="cp-1", provider=ChannelProviderType.SLACK, external_id="C1")
    a = await slack.factory_mod._slack_factory(_slack_provider(), ch, None, owns_inbound=False)
    assert len(slack.apps) == 1 and slack.connected == [] and slack.apps[0].handlers == []
    await a.aclose()
    assert slack.closed == ["xoxb-OLD-aaaaaaaaaaaa"]


async def test_W7_the_outbound_only_slack_app_has_no_socket_mode_handler():
    from primer.channel.slack.connection import _build_outbound_app

    conn = _build_outbound_app(_slack_provider().config)
    assert conn.handler is None and conn.app.client.token == "xoxb-OLD-aaaaaaaaaaaa"
    await conn.start_async()
    await conn.close_async()


async def test_W7_an_outbound_only_discord_connection_logs_in_without_a_gateway(monkeypatch):
    from primer.channel.discord import connection as dc_conn
    from primer.channel.discord import factory as dc_factory
    from primer.model.channel import DiscordChannelProviderConfig
    from tests.channel.discord.test_gateway_start_cleans_up import _Client

    monkeypatch.setattr(dc_factory, "_HANDLERS_INSTALLED", set())
    monkeypatch.setattr(dc_conn.DISCORD_CONNECTIONS, "_entries", {})
    client = _Client(ready=True)
    client.on_message = client.on_interaction = None
    monkeypatch.setattr(dc_conn, "_build_client", lambda cfg: client)
    provider = ChannelProvider(id="cp-dc", provider=ChannelProviderType.DISCORD,
                               config=DiscordChannelProviderConfig(bot_token=SecretStr("x" * 60)))
    ch = Channel(id="ch-1", provider_id="cp-dc", provider=ChannelProviderType.DISCORD, external_id="500")
    a = await dc_factory._discord_factory(provider, ch, None, owns_inbound=False)
    assert client.logged_in and not client.connect_started.is_set()
    assert client.on_message is None and client.on_interaction is None
    await a.aclose()
    assert client.closed == 1 and dc_conn.DISCORD_CONNECTIONS.entry("cp-dc") is None


# ---- W37: a slow update in one chat does not hold up another ----------------------------------------------------------


async def test_W37_a_slow_update_in_chat_a_does_not_delay_chat_b():
    from primer.channel.telegram.connection import _build_application

    processor = _build_application(TelegramChannelProviderConfig(bot_token=SecretStr(_TG_TOKEN))).update_processor
    release_a, b_done = asyncio.Event(), asyncio.Event()

    async def _slow():
        await release_a.wait()

    async def _fast():
        b_done.set()

    a = asyncio.create_task(processor.process_update(_update(1), _slow()))
    b = asyncio.create_task(processor.process_update(_update(2), _fast()))
    try:
        await asyncio.wait_for(b_done.wait(), timeout=2)
    finally:
        release_a.set()
        await asyncio.wait_for(asyncio.gather(a, b), timeout=5)


# ---- W38: a 429 is retried --------------------------------------------------------------------------------------------


async def test_W38_a_telegram_post_answered_429_once_is_retried_after_the_capped_delay(monkeypatch):
    import telegram
    from telegram.error import RetryAfter

    from primer.channel.telegram import connection as tg_conn

    monkeypatch.setattr(tg_conn, "_RETRY_AFTER_CAP_S", 0.01)
    calls: list[str] = []

    async def _do_post(self, endpoint, data, **_kw):
        calls.append(endpoint)
        if len(calls) == 1:
            raise RetryAfter(30)
        return {"message_id": 7, "date": 0, "chat": {"id": 1, "type": "private"}, "text": data.get("text")}

    monkeypatch.setattr(telegram.Bot, "_do_post", _do_post)
    app = tg_conn._build_application(TelegramChannelProviderConfig(bot_token=SecretStr(_TG_TOKEN)))
    msg = await asyncio.wait_for(app.bot.send_message(chat_id=1, text="hi"), timeout=5)
    assert msg.message_id == 7 and calls == ["sendMessage", "sendMessage"]


async def test_W38_a_telegram_post_answered_429_twice_raises(monkeypatch):
    import telegram
    from telegram.error import RetryAfter

    from primer.channel.telegram import connection as tg_conn

    monkeypatch.setattr(tg_conn, "_RETRY_AFTER_CAP_S", 0.01)
    calls: list[str] = []

    async def _do_post(self, endpoint, data, **_kw):
        calls.append(endpoint)
        raise RetryAfter(1)

    monkeypatch.setattr(telegram.Bot, "_do_post", _do_post)
    app = tg_conn._build_application(TelegramChannelProviderConfig(bot_token=SecretStr(_TG_TOKEN)))
    with pytest.raises(RetryAfter):
        await asyncio.wait_for(app.bot.send_message(chat_id=1, text="hi"), timeout=5)
    assert calls == ["sendMessage", "sendMessage"]


async def test_W38_the_slack_web_client_retries_rate_limited_calls_twice():
    from slack_sdk.http_retry.builtin_async_handlers import AsyncRateLimitErrorRetryHandler

    from primer.channel.slack.connection import _build_outbound_app

    conn = _build_outbound_app(_slack_provider().config)
    rate = [h for h in conn.app.client.retry_handlers if isinstance(h, AsyncRateLimitErrorRetryHandler)]
    assert len(rate) == 1 and rate[0].max_retry_count == 2
