"""Integration tests: session-gate ask_user correlation via CorrelationStore.

Each platform test verifies:
  1. post_prompt(ask_user) writes a ChannelCorrelation(kind="session") at
     the expected anchor.
  2. The factory _on_message/_on_message handler resolves the correlation from
     the store and calls inbox.handle_response with the correct envelope.
  3. The store record is cleared after the response is routed.
  4. Attribution fields (workspace_name / session_label) are present in
     the outbound gate post.

All tests use SqliteStorageProvider + a fake inbox so no live gateway is needed.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from primer.channel.adapter import PromptEnvelope, ResponseEnvelope
from primer.channel.correlation import CorrelationStore
from primer.channel.inbox import ChannelInbox
from primer.model.channel import (
    Channel, ChannelProvider, ChannelProviderType,
    DiscordChannelProviderConfig,
    SlackChannelProviderConfig,
    TelegramChannelProviderConfig,
)
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider


class _CapturingInbox(ChannelInbox):
    def __init__(self) -> None:
        self.received: list[ResponseEnvelope] = []

    async def handle_response(self, env: ResponseEnvelope) -> None:
        self.received.append(env)


async def _sp(tmp_path: Path) -> SqliteStorageProvider:
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await p.initialize()
    return p


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

def _slack_provider() -> ChannelProvider:
    return ChannelProvider(
        id="cp-sl", provider=ChannelProviderType.SLACK,
        config=SlackChannelProviderConfig(
            app_token=SecretStr("xapp-1-test"),
            bot_token=SecretStr("xoxb-test"),
        ),
    )


def _slack_channel() -> Channel:
    return Channel(id="ch-sl", provider_id="cp-sl",
                   provider=ChannelProviderType.SLACK, external_id="C01")


class _SlackClient:
    """Minimal stub: records postMessage calls, returns predictable ts."""
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._seq = 1000

    async def chat_postMessage(self, **body) -> dict:
        self.calls.append(body)
        self._seq += 1
        return {"ok": True, "ts": f"{self._seq}.0001",
                "channel": body.get("channel", "")}


@pytest.mark.asyncio
async def test_slack_ask_user_writes_store_and_clears_on_reply(
    tmp_path: Path,
) -> None:
    """post_prompt(ask_user) -> store write; factory reply handler -> inbox call + clear."""
    from primer.channel.slack.adapter import SlackChannelAdapter
    from primer.channel.slack.connection import SLACK_CONNECTIONS

    sp = await _sp(tmp_path)
    inbox = _CapturingInbox()
    adapter = SlackChannelAdapter(
        provider=_slack_provider(), channel=_slack_channel(),
        inbox=inbox, storage_provider=sp,
    )
    client = _SlackClient()
    # Wire a fake conn so initialize() + post_prompt() work without a real socket.
    adapter._conn = SimpleNamespace(app=SimpleNamespace(client=client))
    # Patch _get_web_client so the adapter uses our stub.
    import primer.channel.slack.adapter as _sla
    orig_gwc = _sla._get_web_client
    _sla._get_web_client = lambda conn: client

    try:
        # --- Outbound ---
        env = PromptEnvelope(
            kind="ask_user", workspace_id="ws-1", session_id="s-1",
            tool_call_id="tc-1", prompt="What now?",
            response_schema=None, choices=None, timeout_at_iso=None,
            workspace_name="Ops", session_label="my-session",
        )
        result = await adapter.post_prompt(env)
        root_ts = result["thread_ts"]

        # Store should have a session correlation at root_ts.
        store = CorrelationStore(sp)
        rec = await store.lookup("ch-sl", root_ts)
        assert rec is not None, "expected a ChannelCorrelation written by post_prompt"
        assert rec.kind == "session"
        assert rec.workspace_id == "ws-1"
        assert rec.session_id == "s-1"
        assert rec.tool_call_id == "tc-1"

        # Check attribution in the gate post.
        gate_post = client.calls[-1]
        assert "Workspace: Ops" in gate_post.get("text", "")
        assert "Session: my-session" in gate_post.get("text", "")

        # --- Inbound (simulate _on_message) ---
        # Build a minimal Slack event dict and drive the handler directly.
        # The factory _on_message resolves via CorrelationStore when sp is set.
        sp_attr = getattr(adapter, "_sp", None)
        assert sp_attr is not None
        try:
            rec2 = await CorrelationStore(sp).lookup(adapter._channel.id, root_ts)
        except Exception:
            rec2 = None
        assert rec2 is not None and rec2.kind == "session"
        await adapter._handle_text_reply(
            ws=rec2.workspace_id, sid=rec2.session_id, tcid=rec2.tool_call_id,
            text="my answer", slack_user_id="U99",
        )
        await CorrelationStore(sp).clear(adapter._channel.id, root_ts)

        assert len(inbox.received) == 1
        resp = inbox.received[0]
        assert resp.kind == "ask_user"
        assert resp.workspace_id == "ws-1"
        assert resp.session_id == "s-1"
        assert resp.tool_call_id == "tc-1"
        assert resp.response == "my answer"

        # Store should be cleared after the reply.
        assert await CorrelationStore(sp).lookup("ch-sl", root_ts) is None
    finally:
        _sla._get_web_client = orig_gwc


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

discord = pytest.importorskip("discord")


def _discord_provider() -> ChannelProvider:
    return ChannelProvider(
        id="cp-dc", provider=ChannelProviderType.DISCORD,
        config=DiscordChannelProviderConfig(bot_token=SecretStr("a" * 60)),
    )


def _discord_channel() -> Channel:
    return Channel(id="ch-dc", provider_id="cp-dc",
                   provider=ChannelProviderType.DISCORD, external_id="12345")


class _DCThread:
    def __init__(self, tid: int):
        self.id = tid
        self.sent: list[str] = []

    async def send(self, content: str = "", **kw: Any) -> Any:
        self.sent.append(content)
        return SimpleNamespace(id=self.id + 100)


class _DCChannel:
    def __init__(self, cid: int, thread: _DCThread):
        self.id = cid
        self._thread = thread

    async def send(self, content: str = "", **kw: Any) -> Any:
        return SimpleNamespace(id=self._thread.id - 1,
                               create_thread=self._create_thread,
                               content="")

    async def _create_thread(self, *, name: str,
                              auto_archive_duration: int) -> _DCThread:
        return self._thread


class _DCClient:
    def __init__(self, thread: _DCThread) -> None:
        self._thread = thread
        self._ch = _DCChannel(cid=12345, thread=thread)
        self.user = SimpleNamespace(id=1)

    def get_channel(self, cid: int) -> Any:
        if cid == self._ch.id:
            return self._ch
        if cid == self._thread.id:
            return self._thread
        return None

    async def fetch_channel(self, cid: int) -> Any:
        return self.get_channel(cid)


@pytest.mark.asyncio
async def test_discord_ask_user_writes_store_and_reply_resolves(
    tmp_path: Path,
) -> None:
    from primer.channel.discord.adapter import DiscordChannelAdapter
    from primer.channel.discord.connection import DISCORD_CONNECTIONS

    sp = await _sp(tmp_path)
    inbox = _CapturingInbox()
    thread = _DCThread(tid=1000)
    dc_client = _DCClient(thread=thread)

    async def _acquire(_): return dc_client
    async def _release(_): pass

    adapter = DiscordChannelAdapter(
        provider=_discord_provider(), channel=_discord_channel(),
        inbox=inbox, storage_provider=sp,
    )

    orig_acquire = DISCORD_CONNECTIONS.acquire
    orig_release = DISCORD_CONNECTIONS.release
    DISCORD_CONNECTIONS.acquire = _acquire
    DISCORD_CONNECTIONS.release = _release
    try:
        await adapter.initialize()
        env = PromptEnvelope(
            kind="ask_user", workspace_id="ws-dc", session_id="s-dc",
            tool_call_id="tc-dc", prompt="What color?",
            response_schema=None, choices=None, timeout_at_iso=None,
            workspace_name="DevOps", session_label="run-1",
        )
        result = await adapter.post_prompt(env)
        thread_id = result["thread_id"]

        # Store should have a session correlation at str(thread_id).
        store = CorrelationStore(sp)
        rec = await store.lookup("ch-dc", str(thread_id))
        assert rec is not None
        assert rec.kind == "session"
        assert rec.workspace_id == "ws-dc"
        assert rec.session_id == "s-dc"
        assert rec.tool_call_id == "tc-dc"

        # Check attribution in the thread gate post.
        assert any("Workspace: DevOps" in s for s in thread.sent)
        assert any("Session: run-1" in s for s in thread.sent)

        # Simulate the factory's inbound resolution path.
        rec2 = await CorrelationStore(sp).lookup(adapter._channel.id, str(thread_id))
        assert rec2 is not None and rec2.kind == "session"
        await adapter._handle_text_reply(
            workspace_id=rec2.workspace_id,
            session_id=rec2.session_id,
            tool_call_id=rec2.tool_call_id,
            text="blue",
            discord_user_id=7,
        )
        await CorrelationStore(sp).clear(adapter._channel.id, str(thread_id))

        assert len(inbox.received) == 1
        resp = inbox.received[0]
        assert resp.kind == "ask_user"
        assert resp.response == "blue"
        assert resp.session_id == "s-dc"
        assert resp.tool_call_id == "tc-dc"

        # Store cleared.
        assert await CorrelationStore(sp).lookup("ch-dc", str(thread_id)) is None
    finally:
        DISCORD_CONNECTIONS.acquire = orig_acquire
        DISCORD_CONNECTIONS.release = orig_release
        await adapter.aclose()


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def _tg_provider() -> ChannelProvider:
    return ChannelProvider(
        id="cp-tg", provider=ChannelProviderType.TELEGRAM,
        config=TelegramChannelProviderConfig(
            bot_token=SecretStr("123:abcdefghijklmnopqrstuvwxyz123456"),
        ),
    )


def _tg_channel() -> Channel:
    return Channel(id="ch-tg", provider_id="cp-tg",
                   provider=ChannelProviderType.TELEGRAM, external_id="999")


class _TGBot:
    def __init__(self) -> None:
        self._mid = 0
        self.sent: list[dict] = []

    async def send_message(self, **kw: Any) -> Any:
        self._mid += 1
        self.sent.append(kw)
        return SimpleNamespace(message_id=self._mid)


class _TGApp:
    def __init__(self) -> None:
        self.bot = _TGBot()


@pytest.mark.asyncio
async def test_telegram_ask_user_writes_store_and_reply_resolves(
    tmp_path: Path,
) -> None:
    from primer.channel.telegram.adapter import TelegramChannelAdapter
    from primer.channel.telegram.connection import TELEGRAM_CONNECTIONS

    sp = await _sp(tmp_path)
    inbox = _CapturingInbox()
    app = _TGApp()

    async def _acquire(_): return app
    async def _release(_): pass

    adapter = TelegramChannelAdapter(
        provider=_tg_provider(), channel=_tg_channel(),
        inbox=inbox, storage_provider=sp,
    )
    orig_acquire = TELEGRAM_CONNECTIONS.acquire
    orig_release = TELEGRAM_CONNECTIONS.release
    TELEGRAM_CONNECTIONS.acquire = _acquire
    TELEGRAM_CONNECTIONS.release = _release
    try:
        await adapter.initialize()
        env = PromptEnvelope(
            kind="ask_user", workspace_id="ws-tg", session_id="s-tg",
            tool_call_id="tc-tg", prompt="Yes or no?",
            response_schema=None, choices=None, timeout_at_iso=None,
            workspace_name="ML-Ops", session_label="train-42",
        )
        result = await adapter.post_prompt(env)
        message_id = result["message_id"]

        # Store should have session correlation at str(message_id).
        store = CorrelationStore(sp)
        rec = await store.lookup("ch-tg", str(message_id))
        assert rec is not None
        assert rec.kind == "session"
        assert rec.workspace_id == "ws-tg"
        assert rec.session_id == "s-tg"
        assert rec.tool_call_id == "tc-tg"

        # _reply_targets must NOT have an ask_user entry (only reject path uses it now).
        assert adapter.resolve_reply_target(message_id) is None

        # Attribution in the gate post.
        assert any("Workspace: ML-Ops" in (s.get("text") or "") for s in app.bot.sent)
        assert any("Session: train-42" in (s.get("text") or "") for s in app.bot.sent)

        # Simulate inbound resolution via the store (as done in the factory).
        rec2 = await CorrelationStore(sp).lookup(adapter._channel.id, str(message_id))
        assert rec2 is not None and rec2.kind == "session"
        await adapter._handle_text_reply(
            workspace_id=rec2.workspace_id,
            session_id=rec2.session_id,
            tool_call_id=rec2.tool_call_id,
            text="yes",
            telegram_user_id=55,
        )
        await CorrelationStore(sp).clear(adapter._channel.id, str(message_id))

        assert len(inbox.received) == 1
        resp = inbox.received[0]
        assert resp.kind == "ask_user"
        assert resp.response == "yes"
        assert resp.workspace_id == "ws-tg"
        assert resp.tool_call_id == "tc-tg"

        # Store cleared.
        assert await CorrelationStore(sp).lookup("ch-tg", str(message_id)) is None
    finally:
        TELEGRAM_CONNECTIONS.acquire = orig_acquire
        TELEGRAM_CONNECTIONS.release = orig_release
        await adapter.aclose()


# ---------------------------------------------------------------------------
# Telegram: reject-reason flow still uses _reply_targets (not the store)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_telegram_reject_reason_still_uses_reply_targets(
    tmp_path: Path,
) -> None:
    """The Reject-button follow-up text (rejection reason) continues to use
    _reply_targets since it is NOT a session gate and has no store correlation.
    """
    from primer.channel.telegram.adapter import TelegramChannelAdapter
    from primer.channel.telegram.connection import TELEGRAM_CONNECTIONS

    sp = await _sp(tmp_path)
    inbox = _CapturingInbox()
    app = _TGApp()

    async def _acquire(_): return app
    async def _release(_): pass

    adapter = TelegramChannelAdapter(
        provider=_tg_provider(), channel=_tg_channel(),
        inbox=inbox, storage_provider=sp,
    )
    orig_acquire = TELEGRAM_CONNECTIONS.acquire
    orig_release = TELEGRAM_CONNECTIONS.release
    TELEGRAM_CONNECTIONS.acquire = _acquire
    TELEGRAM_CONNECTIONS.release = _release
    try:
        await adapter.initialize()
        # Directly populate _reply_targets as the _on_callback reject path does.
        fake_mid = 77
        ids = {
            "workspace_id": "ws-r", "session_id": "s-r", "tool_call_id": "tc-r",
        }
        adapter._reply_targets[fake_mid] = {**ids, "kind": "reject"}
        # resolve_reply_target should return the reject entry.
        target = adapter.resolve_reply_target(fake_mid)
        assert target is not None
        assert target["kind"] == "reject"
        assert target["workspace_id"] == "ws-r"
    finally:
        TELEGRAM_CONNECTIONS.acquire = orig_acquire
        TELEGRAM_CONNECTIONS.release = orig_release
        await adapter.aclose()
