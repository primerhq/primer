"""Unit tests for the Discord adapter factory and installed gateway handlers.

Covers ``primer.channel.discord.factory``: ``_route_channel_event``, the
``on_message`` / ``on_interaction`` gateway handlers, the application-command
callbacks (``/agent``, ``/help``, autocomplete), the tree-sync ``on_ready``
hook, and the ``_discord_factory`` builder.

A real (never-connected) ``discord.Client`` is used so the CommandTree and the
ui.View / ui.Modal builders behave exactly as in production; the shared
connection registry and the storage-backed command handlers are faked.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

discord = pytest.importorskip("discord")
from discord import app_commands

from pydantic import SecretStr

from primer.channel.discord import factory as discord_factory
from primer.channel.commands import CommandResult
from primer.model.channel import (
    Channel,
    ChannelProvider,
    ChannelProviderType,
    DiscordChannelProviderConfig,
)


# --------------------------------------------------------------------------- #
# Fakes / helpers
# --------------------------------------------------------------------------- #
def _provider(pid: str = "cp-discord") -> ChannelProvider:
    return ChannelProvider(
        id=pid,
        provider=ChannelProviderType.DISCORD,
        config=DiscordChannelProviderConfig(
            bot_token=SecretStr("x" * 40)),
    )


def _channel(cid: str = "ch-1", ext: str = "500") -> Channel:
    return Channel(
        id=cid, provider_id="cp-discord",
        provider=ChannelProviderType.DISCORD, external_id=ext,
    )


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
    return f"discord-prov-{_UID[0]}"


def _fake_thread(tid: int, parent_id: int):
    """A real ``discord.Thread`` instance (isinstance passes) without running
    its heavy __init__ — a no-__slots__ subclass gives it a writable __dict__."""
    cls = type("_FakeThread", (discord.Thread,), {})
    obj = cls.__new__(cls)
    obj.id = tid
    obj.parent_id = parent_id
    return obj


def _fake_dm(cid: int = 321):
    cls = type("_FakeDM", (discord.DMChannel,), {})
    obj = cls.__new__(cls)
    obj.id = cid
    return obj


def _mock_adapter(sp: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        _sp=object() if sp else None,
        _channel=_channel(),
        _handle_decision=AsyncMock(),
        _handle_text_reply=AsyncMock(),
        handle_inbound_chat_message=AsyncMock(),
    )


def _client():
    return discord.Client(intents=discord.Intents.none())


def _install(monkeypatch, entry, channel=None):
    pid = _uid()
    monkeypatch.setattr(
        discord_factory, "DISCORD_CONNECTIONS", _FakeRegistry(entry))
    client = _client()
    discord_factory._install_handlers(pid, client, channel or _channel())
    return pid, client


def _install_tree(monkeypatch, entry, channel=None):
    pid = _uid()
    monkeypatch.setattr(
        discord_factory, "DISCORD_CONNECTIONS", _FakeRegistry(entry))
    captured: list = []
    real_cls = app_commands.CommandTree

    def _cap(client, *a, **k):
        tr = real_cls(client, *a, **k)
        captured.append(tr)
        return tr

    monkeypatch.setattr(discord_factory.app_commands, "CommandTree", _cap)
    client = _client()
    discord_factory._install_handlers(pid, client, channel or _channel())
    return pid, client, captured[0]


def _message(channel, content="hi", bot=False, mid=42):
    author = SimpleNamespace(bot=bot, id=1, display_name="Dee", name="dee")
    return SimpleNamespace(
        channel=channel, author=author, id=mid,
        content=content, attachments=[])


def _interaction(custom_id=None, parent_id=None, channel_id=100,
                 msg_content="body", user_id=7):
    data = {"custom_id": custom_id} if custom_id is not None else {}
    return SimpleNamespace(
        data=data,
        channel=SimpleNamespace(parent_id=parent_id),
        channel_id=channel_id,
        response=SimpleNamespace(
            edit_message=AsyncMock(), send_modal=AsyncMock(),
            send_message=AsyncMock()),
        message=SimpleNamespace(content=msg_content),
        user=SimpleNamespace(id=user_id),
    )


# --------------------------------------------------------------------------- #
# _route_channel_event
# --------------------------------------------------------------------------- #
def _patch_normalizer(monkeypatch, result):
    class _N:
        def __init__(self, provider_id):
            pass

        async def normalize(self, envelope):
            return result

    monkeypatch.setattr(
        "primer.channel.discord.normalizer.DiscordEventNormalizer", _N)


@pytest.mark.asyncio
async def test_route_channel_event_no_router_returns_false():
    adapter = SimpleNamespace(_event_router=lambda: None, _channel=_channel())
    msg = _message(SimpleNamespace(id=1))
    assert await discord_factory._route_channel_event(adapter, "p", msg) is False


@pytest.mark.asyncio
async def test_route_channel_event_normalized_none(monkeypatch):
    router = SimpleNamespace(
        has_matching_rule=AsyncMock(return_value=True), route_event=AsyncMock())
    adapter = SimpleNamespace(_event_router=lambda: router, _channel=_channel())
    _patch_normalizer(monkeypatch, None)
    msg = _message(SimpleNamespace(id=1))
    assert await discord_factory._route_channel_event(adapter, "p", msg) is False
    router.has_matching_rule.assert_not_awaited()


@pytest.mark.asyncio
async def test_route_channel_event_text_no_match(monkeypatch):
    router = SimpleNamespace(
        has_matching_rule=AsyncMock(return_value=False), route_event=AsyncMock())
    adapter = SimpleNamespace(_event_router=lambda: router, _channel=_channel())
    _patch_normalizer(monkeypatch, {"norm": True})
    msg = _message(SimpleNamespace(id=9), content="text")
    assert await discord_factory._route_channel_event(adapter, "p", msg) is False
    router.route_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_route_channel_event_thread_match_dispatches(monkeypatch):
    router = SimpleNamespace(
        has_matching_rule=AsyncMock(return_value=True), route_event=AsyncMock())
    adapter = SimpleNamespace(_event_router=lambda: router, _channel=_channel())
    _patch_normalizer(monkeypatch, {"norm": True})
    msg = _message(_fake_thread(11, 22))
    assert await discord_factory._route_channel_event(adapter, "p", msg) is True
    router.route_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_route_channel_event_dm_channel_kind(monkeypatch):
    router = SimpleNamespace(
        has_matching_rule=AsyncMock(return_value=True), route_event=AsyncMock())
    adapter = SimpleNamespace(_event_router=lambda: router, _channel=_channel())
    captured: dict = {}

    class _N:
        def __init__(self, provider_id):
            pass

        async def normalize(self, envelope):
            captured.update(envelope["payload"]["channel"])
            return {"norm": True}

    monkeypatch.setattr(
        "primer.channel.discord.normalizer.DiscordEventNormalizer", _N)
    msg = _message(_fake_dm())
    assert await discord_factory._route_channel_event(adapter, "p", msg) is True
    assert captured["kind"] == "dm"


@pytest.mark.asyncio
async def test_route_channel_event_swallows_exception(monkeypatch):
    router = SimpleNamespace(
        has_matching_rule=AsyncMock(side_effect=RuntimeError("boom")),
        route_event=AsyncMock())
    adapter = SimpleNamespace(_event_router=lambda: router, _channel=_channel())
    _patch_normalizer(monkeypatch, {"norm": True})
    msg = _message(SimpleNamespace(id=1))
    assert await discord_factory._route_channel_event(adapter, "p", msg) is False


# --------------------------------------------------------------------------- #
# on_message
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_on_message_ignores_bot(monkeypatch):
    adapter = _mock_adapter()
    _, client = _install(monkeypatch, _FakeEntry({"555": adapter}))
    await client.on_message(_message(SimpleNamespace(id=555), bot=True))
    adapter.handle_inbound_chat_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_message_toplevel_chat_dispatch(monkeypatch):
    adapter = _mock_adapter()
    _, client = _install(monkeypatch, _FakeEntry({"555": adapter}))
    monkeypatch.setattr(
        discord_factory, "_route_channel_event", AsyncMock(return_value=False))
    await client.on_message(_message(SimpleNamespace(id=555), content="hey", mid=42))
    adapter.handle_inbound_chat_message.assert_awaited_once()
    kw = adapter.handle_inbound_chat_message.await_args.kwargs
    assert kw["thread_id"] is None
    assert kw["message_id"] == "42"
    assert kw["sender_name"] == "Dee"
    assert kw["text"] == "hey"


@pytest.mark.asyncio
async def test_on_message_toplevel_rule_match_skips(monkeypatch):
    adapter = _mock_adapter()
    _, client = _install(monkeypatch, _FakeEntry({"555": adapter}))
    monkeypatch.setattr(
        discord_factory, "_route_channel_event", AsyncMock(return_value=True))
    await client.on_message(_message(SimpleNamespace(id=555)))
    adapter.handle_inbound_chat_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_message_toplevel_without_storage_is_noop(monkeypatch):
    adapter = _mock_adapter(sp=False)
    _, client = _install(monkeypatch, _FakeEntry({"555": adapter}))
    await client.on_message(_message(SimpleNamespace(id=555)))
    adapter.handle_inbound_chat_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_message_unknown_entry_is_noop(monkeypatch):
    _, client = _install(monkeypatch, None)  # registry.entry -> None
    # Should simply return without raising.
    await client.on_message(_message(SimpleNamespace(id=1)))


@pytest.mark.asyncio
async def test_on_message_thread_session_reply(monkeypatch):
    adapter = _mock_adapter()
    _, client = _install(monkeypatch, _FakeEntry({"999": adapter}))
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
    msg = _message(_fake_thread(777, 999), content="answer", mid=8)
    await client.on_message(msg)
    adapter._handle_text_reply.assert_awaited_once()
    assert adapter._handle_text_reply.await_args.kwargs["text"] == "answer"
    assert cleared == ["777"]
    adapter.handle_inbound_chat_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_message_thread_chat_dispatch(monkeypatch):
    adapter = _mock_adapter()
    _, client = _install(monkeypatch, _FakeEntry({"999": adapter}))

    class Corr:
        def __init__(self, sp):
            pass

        async def lookup(self, cid, key):
            return None

    monkeypatch.setattr("primer.channel.correlation.CorrelationStore", Corr)
    monkeypatch.setattr(
        discord_factory, "_route_channel_event", AsyncMock(return_value=False))
    msg = _message(_fake_thread(777, 999), content="chat", mid=8)
    await client.on_message(msg)
    adapter.handle_inbound_chat_message.assert_awaited_once()
    kw = adapter.handle_inbound_chat_message.await_args.kwargs
    assert kw["thread_id"] == "777"
    assert kw["message_id"] == "8"


@pytest.mark.asyncio
async def test_on_message_thread_unknown_parent_is_noop(monkeypatch):
    adapter = _mock_adapter()
    _, client = _install(monkeypatch, _FakeEntry({"999": adapter}))
    # parent_id 12345 is not registered -> adapter None -> return.
    await client.on_message(_message(_fake_thread(1, 12345)))
    adapter.handle_inbound_chat_message.assert_not_awaited()
    adapter._handle_text_reply.assert_not_awaited()


# --------------------------------------------------------------------------- #
# on_interaction
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_on_interaction_no_custom_id_is_noop(monkeypatch):
    adapter = _mock_adapter()
    _, client = _install(monkeypatch, _FakeEntry({"100": adapter}))
    await client.on_interaction(_interaction(custom_id=None))
    adapter._handle_decision.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_interaction_undecodable_custom_id_is_noop(monkeypatch):
    adapter = _mock_adapter()
    _, client = _install(monkeypatch, _FakeEntry({"100": adapter}))
    await client.on_interaction(_interaction(custom_id="garbage"))
    adapter._handle_decision.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_interaction_approve(monkeypatch):
    adapter = _mock_adapter()
    _, client = _install(monkeypatch, _FakeEntry({"100": adapter}))
    inter = _interaction(custom_id="approve:w:s:t", parent_id=None, channel_id=100)
    await client.on_interaction(inter)
    inter.response.edit_message.assert_awaited_once()
    adapter._handle_decision.assert_awaited_once()
    kw = adapter._handle_decision.await_args.kwargs
    assert kw["decision"] == "approved"
    assert kw["workspace_id"] == "w"
    assert kw["user_id"] == 7


@pytest.mark.asyncio
async def test_on_interaction_approve_resolves_thread_parent(monkeypatch):
    adapter = _mock_adapter()
    _, client = _install(monkeypatch, _FakeEntry({"555": adapter}))
    # parent_id set -> adapter resolved via the thread's parent channel id.
    inter = _interaction(custom_id="approve:w:s:t", parent_id=555)
    await client.on_interaction(inter)
    adapter._handle_decision.assert_awaited_once()


@pytest.mark.asyncio
async def test_on_interaction_approve_unknown_channel_is_noop(monkeypatch):
    _, client = _install(monkeypatch, _FakeEntry({}))
    inter = _interaction(custom_id="approve:w:s:t")
    await client.on_interaction(inter)
    inter.response.edit_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_interaction_reject_opens_modal(monkeypatch):
    adapter = _mock_adapter()
    _, client = _install(monkeypatch, _FakeEntry({"100": adapter}))
    inter = _interaction(custom_id="reject:w:s:t", channel_id=100)
    await client.on_interaction(inter)
    inter.response.send_modal.assert_awaited_once()
    modal = inter.response.send_modal.await_args.args[0]
    assert "reject" in modal.custom_id


@pytest.mark.asyncio
async def test_on_interaction_reject_unknown_channel_is_noop(monkeypatch):
    _, client = _install(monkeypatch, _FakeEntry({}))
    inter = _interaction(custom_id="reject:w:s:t")
    await client.on_interaction(inter)
    inter.response.send_modal.assert_not_awaited()


# --------------------------------------------------------------------------- #
# /agent application command
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_cmd_agent_not_configured(monkeypatch):
    _, _, tree = _install_tree(monkeypatch, _FakeEntry({}))
    cmd = tree.get_command("agent")
    inter = _interaction(channel_id=1)
    await cmd.callback(inter, value="")
    inter.response.send_message.assert_awaited_once()
    assert "not configured" in inter.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_cmd_agent_requires_thread(monkeypatch):
    adapter = _mock_adapter()
    _, _, tree = _install_tree(monkeypatch, _FakeEntry({"1": adapter}))
    cmd = tree.get_command("agent")
    inter = _interaction(channel_id=1)  # channel is not a Thread
    await cmd.callback(inter, value="")
    msg = inter.response.send_message.await_args.args[0]
    assert "chat thread" in msg


@pytest.mark.asyncio
async def test_cmd_agent_notice(monkeypatch):
    adapter = _mock_adapter()
    _, _, tree = _install_tree(monkeypatch, _FakeEntry({"1": adapter}))
    monkeypatch.setattr(
        discord_factory, "handle_app_command",
        AsyncMock(return_value=CommandResult(kind="notice", text="Switched.")))
    cmd = tree.get_command("agent")
    inter = _interaction()
    inter.channel = _fake_thread(50, 1)
    await cmd.callback(inter, value="a1")
    inter.response.send_message.assert_awaited_once()
    assert inter.response.send_message.await_args.args[0] == "Switched."


@pytest.mark.asyncio
async def test_cmd_agent_renders_picker(monkeypatch):
    adapter = _mock_adapter()
    _, _, tree = _install_tree(monkeypatch, _FakeEntry({"1": adapter}))
    monkeypatch.setattr(
        discord_factory, "handle_app_command",
        AsyncMock(return_value=CommandResult(
            kind="agent_picker",
            items=[{"label": "Agent A", "agent_id": "a1"}])))
    cmd = tree.get_command("agent")
    inter = _interaction()
    inter.channel = _fake_thread(50, 1)
    await cmd.callback(inter, value="")
    inter.response.send_message.assert_awaited_once()
    assert "view" in inter.response.send_message.await_args.kwargs


@pytest.mark.asyncio
async def test_cmd_agent_picker_empty(monkeypatch):
    adapter = _mock_adapter()
    _, _, tree = _install_tree(monkeypatch, _FakeEntry({"1": adapter}))
    monkeypatch.setattr(
        discord_factory, "handle_app_command",
        AsyncMock(return_value=CommandResult(kind="agent_picker", items=[])))
    cmd = tree.get_command("agent")
    inter = _interaction()
    inter.channel = _fake_thread(50, 1)
    await cmd.callback(inter, value="")
    assert inter.response.send_message.await_args.args[0] == "No agents."


@pytest.mark.asyncio
async def test_cmd_agent_handler_error_surfaced(monkeypatch):
    adapter = _mock_adapter()
    _, _, tree = _install_tree(monkeypatch, _FakeEntry({"1": adapter}))
    monkeypatch.setattr(
        discord_factory, "handle_app_command",
        AsyncMock(side_effect=RuntimeError("boom")))
    cmd = tree.get_command("agent")
    inter = _interaction()
    inter.channel = _fake_thread(50, 1)
    await cmd.callback(inter, value="a1")
    assert inter.response.send_message.await_args.args[0] == "boom"


# --------------------------------------------------------------------------- #
# /help application command
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_cmd_help_notice(monkeypatch):
    adapter = _mock_adapter()
    _, _, tree = _install_tree(monkeypatch, _FakeEntry({"1": adapter}))
    monkeypatch.setattr(
        discord_factory, "handle_app_command",
        AsyncMock(return_value=CommandResult(kind="notice", text="help stuff")))
    cmd = tree.get_command("help")
    inter = _interaction()
    await cmd.callback(inter)
    assert inter.response.send_message.await_args.args[0] == "help stuff"


@pytest.mark.asyncio
async def test_cmd_help_fallback_on_error(monkeypatch):
    _, _, tree = _install_tree(monkeypatch, _FakeEntry({}))
    monkeypatch.setattr(
        discord_factory, "handle_app_command",
        AsyncMock(side_effect=RuntimeError("x")))
    cmd = tree.get_command("help")
    inter = _interaction()
    await cmd.callback(inter)
    text = inter.response.send_message.await_args.args[0]
    assert "Commands:" in text  # local help_text() fallback


# --------------------------------------------------------------------------- #
# agent autocomplete
# --------------------------------------------------------------------------- #
def _autocomplete(tree):
    return tree.get_command("agent")._params["value"].autocomplete


@pytest.mark.asyncio
async def test_agent_autocomplete_no_storage_returns_empty(monkeypatch):
    _, _, tree = _install_tree(monkeypatch, _FakeEntry({}))
    ac = _autocomplete(tree)
    assert await ac(SimpleNamespace(), "a") == []


@pytest.mark.asyncio
async def test_agent_autocomplete_maps_choices(monkeypatch):
    adapter = _mock_adapter()
    _, _, tree = _install_tree(monkeypatch, _FakeEntry({"1": adapter}))
    monkeypatch.setattr(
        discord_factory, "agent_autocomplete_choices",
        AsyncMock(return_value=[{"name": "Agent A", "value": "a1"}]))
    ac = _autocomplete(tree)
    out = await ac(SimpleNamespace(), "ag")
    assert len(out) == 1
    assert out[0].name == "Agent A"
    assert out[0].value == "a1"


@pytest.mark.asyncio
async def test_agent_autocomplete_error_returns_empty(monkeypatch):
    adapter = _mock_adapter()
    _, _, tree = _install_tree(monkeypatch, _FakeEntry({"1": adapter}))
    monkeypatch.setattr(
        discord_factory, "agent_autocomplete_choices",
        AsyncMock(side_effect=RuntimeError("x")))
    ac = _autocomplete(tree)
    assert await ac(SimpleNamespace(), "ag") == []


# --------------------------------------------------------------------------- #
# tree sync via on_ready
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_on_ready_syncs_to_guild(monkeypatch):
    adapter = _mock_adapter()
    _, client, tree = _install_tree(
        monkeypatch, _FakeEntry({"1": adapter}), channel=_channel(ext="500"))
    guild = SimpleNamespace(id=99)
    monkeypatch.setattr(client, "get_channel", lambda cid: SimpleNamespace(guild=guild))
    monkeypatch.setattr(tree, "copy_global_to", MagicMock())
    monkeypatch.setattr(tree, "sync", AsyncMock())
    await client.on_ready()
    tree.copy_global_to.assert_called_once()
    tree.sync.assert_awaited_once()
    # Second on_ready is a no-op (guarded so we sync once per provider).
    await client.on_ready()
    tree.sync.assert_awaited_once()


@pytest.mark.asyncio
async def test_on_ready_global_sync_when_no_guild(monkeypatch):
    _, client, tree = _install_tree(monkeypatch, _FakeEntry({}))
    # get_channel returns an object with no guild attribute -> global sync.
    monkeypatch.setattr(client, "get_channel", lambda cid: SimpleNamespace())
    monkeypatch.setattr(tree, "sync", AsyncMock())
    await client.on_ready()
    tree.sync.assert_awaited_once()


@pytest.mark.asyncio
async def test_on_ready_sync_error_resets_flag(monkeypatch):
    _, client, tree = _install_tree(monkeypatch, _FakeEntry({}))
    monkeypatch.setattr(client, "get_channel", lambda cid: SimpleNamespace())
    calls = {"n": 0}

    async def _boom(*a, **k):
        calls["n"] += 1
        raise RuntimeError("sync failed")

    monkeypatch.setattr(tree, "sync", _boom)
    await client.on_ready()  # error swallowed, flag reset
    await client.on_ready()  # retried because previous attempt failed
    assert calls["n"] == 2


# --------------------------------------------------------------------------- #
# extra branch coverage
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_on_interaction_no_entry_is_noop(monkeypatch):
    _, client = _install(monkeypatch, None)  # registry.entry -> None
    inter = _interaction(custom_id="approve:w:s:t")
    await client.on_interaction(inter)
    inter.response.edit_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_message_thread_without_storage_is_noop(monkeypatch):
    adapter = _mock_adapter(sp=False)
    _, client = _install(monkeypatch, _FakeEntry({"999": adapter}))
    await client.on_message(_message(_fake_thread(777, 999)))
    adapter.handle_inbound_chat_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_message_thread_rule_match_skips(monkeypatch):
    adapter = _mock_adapter()
    _, client = _install(monkeypatch, _FakeEntry({"999": adapter}))

    class Corr:
        def __init__(self, sp):
            pass

        async def lookup(self, cid, key):
            return None

    monkeypatch.setattr("primer.channel.correlation.CorrelationStore", Corr)
    monkeypatch.setattr(
        discord_factory, "_route_channel_event", AsyncMock(return_value=True))
    await client.on_message(_message(_fake_thread(777, 999)))
    adapter.handle_inbound_chat_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_message_thread_lookup_error_falls_through(monkeypatch):
    adapter = _mock_adapter()
    _, client = _install(monkeypatch, _FakeEntry({"999": adapter}))

    class Corr:
        def __init__(self, sp):
            pass

        async def lookup(self, cid, key):
            raise RuntimeError("store down")

    monkeypatch.setattr("primer.channel.correlation.CorrelationStore", Corr)
    monkeypatch.setattr(
        discord_factory, "_route_channel_event", AsyncMock(return_value=False))
    await client.on_message(_message(_fake_thread(777, 999), content="c", mid=3))
    adapter.handle_inbound_chat_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_on_message_thread_clear_error_swallowed(monkeypatch):
    adapter = _mock_adapter()
    _, client = _install(monkeypatch, _FakeEntry({"999": adapter}))
    rec = SimpleNamespace(
        kind="session", workspace_id="w", session_id="s", tool_call_id="t")

    class Corr:
        def __init__(self, sp):
            pass

        async def lookup(self, cid, key):
            return rec

        async def clear(self, cid, key):
            raise RuntimeError("clear failed")

    monkeypatch.setattr("primer.channel.correlation.CorrelationStore", Corr)
    await client.on_message(_message(_fake_thread(777, 999), content="a"))
    adapter._handle_text_reply.assert_awaited_once()
    adapter.handle_inbound_chat_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_cmd_agent_no_channel_not_configured(monkeypatch):
    _, _, tree = _install_tree(monkeypatch, _FakeEntry({"1": _mock_adapter()}))
    cmd = tree.get_command("agent")
    inter = _interaction(channel_id=None)  # channel_id None -> parent None
    await cmd.callback(inter, value="")
    assert "not configured" in inter.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_cmd_agent_pick_applies_switch(monkeypatch):
    adapter = _mock_adapter()
    _, _, tree = _install_tree(monkeypatch, _FakeEntry({"1": adapter}))
    handler = AsyncMock(return_value=CommandResult(
        kind="agent_picker", items=[{"label": "A", "agent_id": "a1"}]))
    monkeypatch.setattr(discord_factory, "handle_app_command", handler)
    cmd = tree.get_command("agent")
    inter = _interaction()
    inter.channel = _fake_thread(50, 1)
    await cmd.callback(inter, value="")
    view = inter.response.send_message.await_args.kwargs["view"]
    select = view.children[0]
    # Simulate the user choosing agent "a1" from the dropdown.
    handler.return_value = CommandResult(kind="notice", text="Switched to a1.")
    pick_inter = SimpleNamespace(response=SimpleNamespace(
        edit_message=AsyncMock(), send_message=AsyncMock()))
    await select._on_pick(pick_inter, "a1")
    pick_inter.response.edit_message.assert_awaited_once()
    assert "Switched to a1." in pick_inter.response.edit_message.await_args.kwargs["content"]


def test_install_handlers_tree_failure_is_safe(monkeypatch):
    pid = _uid()
    monkeypatch.setattr(
        discord_factory, "DISCORD_CONNECTIONS", _FakeRegistry(_FakeEntry()))

    def _boom(client, *a, **k):
        raise RuntimeError("no tree")

    monkeypatch.setattr(discord_factory.app_commands, "CommandTree", _boom)
    client = _client()
    discord_factory._install_handlers(pid, client, _channel())  # must not raise
    assert hasattr(client, "on_message")  # handlers bound before tree creation


@pytest.mark.asyncio
async def test_on_ready_syncs_via_fetch_channel(monkeypatch):
    _, client, tree = _install_tree(
        monkeypatch, _FakeEntry({}), channel=_channel(ext="500"))
    guild = SimpleNamespace(id=7)
    monkeypatch.setattr(client, "get_channel", lambda cid: None)
    monkeypatch.setattr(
        client, "fetch_channel",
        AsyncMock(return_value=SimpleNamespace(guild=guild)))
    monkeypatch.setattr(tree, "copy_global_to", MagicMock())
    monkeypatch.setattr(tree, "sync", AsyncMock())
    await client.on_ready()
    client.fetch_channel.assert_awaited_once()
    tree.sync.assert_awaited_once()


# --------------------------------------------------------------------------- #
# idempotent install + _discord_factory
# --------------------------------------------------------------------------- #
def test_install_handlers_is_idempotent(monkeypatch):
    pid = _uid()
    monkeypatch.setattr(
        discord_factory, "DISCORD_CONNECTIONS", _FakeRegistry(_FakeEntry()))
    c1 = _client()
    discord_factory._install_handlers(pid, c1, _channel())
    assert hasattr(c1, "on_message")
    c2 = _client()
    discord_factory._install_handlers(pid, c2, _channel())  # same pid
    assert not hasattr(c2, "on_message")


@pytest.mark.asyncio
async def test_discord_factory_builds_and_installs(monkeypatch):
    created: dict = {}

    class _FakeAdapter:
        def __init__(self, **kw):
            created.update(kw)
            self.initialized = False

        async def initialize(self):
            self.initialized = True

    installed: list = []
    monkeypatch.setattr(discord_factory, "DiscordChannelAdapter", _FakeAdapter)
    monkeypatch.setattr(
        discord_factory, "_install_handlers",
        lambda pid, client, channel: installed.append((pid, client, channel)))
    client_obj = object()
    entry = SimpleNamespace(client=client_obj)
    monkeypatch.setattr(
        discord_factory, "DISCORD_CONNECTIONS", _FakeRegistry(entry))

    provider = _provider("cp-d")
    channel = _channel()
    adapter = await discord_factory._discord_factory(
        provider, channel, object(), storage_provider="SP",
        event_bus="EB", claim_engine="CE", artifact_registry="AR")

    assert isinstance(adapter, _FakeAdapter)
    assert adapter.initialized is True
    assert created["provider"] is provider
    assert created["storage_provider"] == "SP"
    assert installed == [(provider.id, client_obj, channel)]


@pytest.mark.asyncio
async def test_discord_factory_without_connection_skips_handlers(monkeypatch):
    class _FakeAdapter:
        def __init__(self, **kw):
            pass

        async def initialize(self):
            pass

    installed: list = []
    monkeypatch.setattr(discord_factory, "DiscordChannelAdapter", _FakeAdapter)
    monkeypatch.setattr(
        discord_factory, "_install_handlers",
        lambda *a: installed.append(a))
    monkeypatch.setattr(
        discord_factory, "DISCORD_CONNECTIONS", _FakeRegistry(None))
    await discord_factory._discord_factory(_provider("cp-e"), _channel(), object())
    assert installed == []
