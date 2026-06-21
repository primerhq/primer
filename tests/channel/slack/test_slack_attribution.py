"""SlackChannelAdapter prepends attribution header on gate posts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from primer.channel.adapter import PromptEnvelope
from primer.channel.slack.adapter import SlackChannelAdapter


class _FakeClient:
    def __init__(self):
        self.post_calls: list[dict] = []
        self._stream_calls: list[str] = []

    async def chat_postMessage(self, **kw):
        self.post_calls.append(kw)
        return {"ts": "t-1", "channel": kw.get("channel", "")}

    async def files_upload_v2(self, **kw):
        pass


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


def _adapter() -> SlackChannelAdapter:
    a = SlackChannelAdapter(
        provider=SimpleNamespace(id="cp"),
        channel=SimpleNamespace(id="ch-1", external_id="C123"),
        inbox=None,
    )
    client = _FakeClient()
    a._conn = SimpleNamespace(app=SimpleNamespace(client=client))
    return a


@pytest.mark.asyncio
async def test_ask_user_contains_attribution():
    a = _adapter()
    env = _make_envelope(workspace_name="Ops", session_label="s-1")
    await a.post_prompt(env)
    client: _FakeClient = a._conn.app.client
    # The last chat_postMessage is the gate post (first is the thread anchor).
    texts = [c["text"] for c in client.post_calls]
    gate_text = texts[-1]
    assert "Workspace: Ops" in gate_text
    assert "Session: s-1" in gate_text


@pytest.mark.asyncio
async def test_no_attribution_when_fields_absent():
    a = _adapter()
    env = _make_envelope()
    await a.post_prompt(env)
    client: _FakeClient = a._conn.app.client
    gate_text = client.post_calls[-1]["text"]
    assert "Workspace:" not in gate_text
    assert "Session:" not in gate_text


@pytest.mark.asyncio
async def test_post_chat_message_no_attribution(monkeypatch):
    """Chat relay must not include attribution."""
    captured: list[str] = []

    async def _fake_stream_or_post(*, client, channel, thread_ts, text, team_id=None):
        captured.append(text)

    import primer.channel.slack.streaming as _streaming_mod
    monkeypatch.setattr(_streaming_mod, "stream_or_post", _fake_stream_or_post)

    a = _adapter()
    await a.post_chat_message("hi")

    assert captured == ["hi"]
    assert "Workspace:" not in captured[0]
