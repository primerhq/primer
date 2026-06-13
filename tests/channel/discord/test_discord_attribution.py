"""DiscordChannelAdapter prepends attribution header on gate posts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from primer.channel.adapter import PromptEnvelope
from primer.channel.discord.adapter import DiscordChannelAdapter


class _FakeThread:
    """Minimal discord thread stub that records send() calls."""

    def __init__(self, tid: int = 42):
        self.id = tid
        self.sent: list[dict] = []

    async def send(self, content: str = "", **kw):
        self.sent.append({"content": content, **kw})
        return SimpleNamespace(id=99)


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


def _adapter(thread: _FakeThread) -> DiscordChannelAdapter:
    """Build an adapter with a fake client; pre-seed the session thread cache
    so _session_thread() skips network calls."""
    a = DiscordChannelAdapter(
        provider=SimpleNamespace(id="cp"),
        channel=SimpleNamespace(id="ch-1", external_id="999"),
        inbox=None,
    )
    # Fake discord client: get_channel returns the pre-seeded thread.
    a._client = SimpleNamespace(get_channel=lambda tid: thread)
    # Pre-seed so _session_thread() returns immediately.
    a._session_threads["sess-1"] = thread.id
    return a


@pytest.mark.asyncio
async def test_ask_user_contains_attribution():
    thread = _FakeThread()
    a = _adapter(thread)
    env = _make_envelope(workspace_name="Ops", session_label="s-1")
    await a.post_prompt(env)
    assert thread.sent, "expected thread.send() to be called"
    content = thread.sent[-1]["content"]
    assert "Workspace: Ops" in content
    assert "Session: s-1" in content


@pytest.mark.asyncio
async def test_no_attribution_when_fields_absent():
    thread = _FakeThread()
    a = _adapter(thread)
    env = _make_envelope()
    await a.post_prompt(env)
    content = thread.sent[-1]["content"]
    assert "Workspace:" not in content
    assert "Session:" not in content


@pytest.mark.asyncio
async def test_post_chat_message_no_attribution():
    """Chat relay (post_chat_message) must not include attribution."""
    thread = _FakeThread()
    a = DiscordChannelAdapter(
        provider=SimpleNamespace(id="cp"),
        channel=SimpleNamespace(id="ch-1", external_id="999"),
        inbox=None,
    )

    # Override _resolve_chat_thread to return our fake thread directly
    # (mirrors the approach in test_discord_outbound_media.py).
    async def _resolve(thread_ts):
        return thread

    a._resolve_chat_thread = _resolve  # type: ignore[assignment]

    await a.post_chat_message("hi")
    assert thread.sent == [{"content": "hi"}]
    assert "Workspace:" not in thread.sent[0]["content"]
