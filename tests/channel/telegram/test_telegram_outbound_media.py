"""TelegramChannelAdapter.post_chat_media dispatches by MIME."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from primer.channel.telegram.adapter import TelegramChannelAdapter
from primer.model.chat import AudioPart, DocumentPart, ImagePart


class _FakeBot:
    def __init__(self):
        self.calls = []

    async def send_photo(self, **kw):
        self.calls.append(("photo", kw))

    async def send_audio(self, **kw):
        self.calls.append(("audio", kw))

    async def send_document(self, **kw):
        self.calls.append(("document", kw))


def _adapter():
    a = TelegramChannelAdapter(
        provider=SimpleNamespace(id="cp"),
        channel=SimpleNamespace(id="ch-1", external_id="555"), inbox=None)
    a._app = SimpleNamespace(bot=_FakeBot())
    return a


@pytest.mark.asyncio
async def test_post_chat_media_dispatches_by_mime():
    a = _adapter()
    res = await a.post_chat_media([
        ImagePart(data=b"PNG", mime_type="image/png"),
        AudioPart(data=b"OGG", mime_type="audio/ogg"),
        DocumentPart(data=b"PDF", mime_type="application/pdf", filename="r.pdf"),
    ])
    kinds = [c[0] for c in a._app.bot.calls]
    assert kinds == ["photo", "audio", "document"]
    assert res["sent"] == 3


@pytest.mark.asyncio
async def test_post_chat_media_skips_parts_without_data():
    a = _adapter()
    res = await a.post_chat_media([
        ImagePart(artifact_id="artifact-x", mime_type="image/png")])
    assert res["sent"] == 0
    assert a._app.bot.calls == []
