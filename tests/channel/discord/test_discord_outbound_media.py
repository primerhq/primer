"""DiscordChannelAdapter.post_chat_media uploads files into the chat thread."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from primer.channel.discord.adapter import DiscordChannelAdapter
from primer.model.chat import DocumentPart, ImagePart


class _FakeThread:
    def __init__(self):
        self.sent = []

    async def send(self, *, file=None, content=None):
        self.sent.append(file)


def _adapter(thread):
    a = DiscordChannelAdapter(
        provider=SimpleNamespace(id="cp"),
        channel=SimpleNamespace(id="ch-1", external_id="999"), inbox=None)

    async def _resolve(thread_ts):
        return thread
    a._resolve_chat_thread = _resolve  # type: ignore[assignment]
    return a


@pytest.mark.asyncio
async def test_post_chat_media_sends_files():
    thread = _FakeThread()
    a = _adapter(thread)
    res = await a.post_chat_media(
        [ImagePart(data=b"PNG", mime_type="image/png"),
         DocumentPart(data=b"PDF", mime_type="application/pdf", filename="r.pdf")],
        thread_ts="123")
    assert len(thread.sent) == 2
    # discord.File objects were constructed (filename preserved on the 2nd).
    assert getattr(thread.sent[1], "filename", None) == "r.pdf"
    assert res["sent"] == 2
