"""Slack streaming relay + full-postMessage fallback."""

from __future__ import annotations

import pytest

from primer.channel.slack.streaming import stream_or_post


class _FakeOK:
    def __init__(self):
        self.calls = []

    async def chat_startStream(self, **kw):
        self.calls.append(("start", kw))
        return {"ts": "1700.1"}

    async def chat_appendStream(self, **kw):
        self.calls.append(("append", kw))
        return {"ok": True}

    async def chat_stopStream(self, **kw):
        self.calls.append(("stop", kw))
        return {"ok": True}

    async def chat_postMessage(self, **kw):
        self.calls.append(("post", kw))
        return {"ts": "1700.9"}


class _FakeNoStream:
    """No streaming methods -> falls back to postMessage."""

    def __init__(self):
        self.calls = []

    async def chat_postMessage(self, **kw):
        self.calls.append(("post", kw))
        return {"ts": "1700.9"}


@pytest.mark.asyncio
async def test_streams_when_supported():
    cli = _FakeOK()
    await stream_or_post(
        client=cli, channel="C1", thread_ts="t-1", text="hello world")
    verbs = [c[0] for c in cli.calls]
    assert verbs == ["start", "append", "stop"]
    assert "post" not in verbs


@pytest.mark.asyncio
async def test_falls_back_to_postmessage():
    cli = _FakeNoStream()
    await stream_or_post(
        client=cli, channel="C1", thread_ts="t-1", text="hello world")
    assert cli.calls == [("post", {
        "channel": "C1", "thread_ts": "t-1", "text": "hello world"})]


@pytest.mark.asyncio
async def test_stream_error_falls_back():
    class _Boom(_FakeOK):
        async def chat_startStream(self, **kw):
            raise RuntimeError("429 rate limited")
    cli = _Boom()
    await stream_or_post(
        client=cli, channel="C1", thread_ts="t-1", text="hi")
    assert cli.calls[-1][0] == "post"
