"""SlackChannelAdapter.post_chat_media uploads via files_upload_v2."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from primer.channel.slack.adapter import SlackChannelAdapter
from primer.model.chat import DocumentPart, ImagePart


class _FakeClient:
    def __init__(self):
        self.uploads = []

    async def files_upload_v2(self, **kw):
        self.uploads.append(kw)


def _adapter():
    a = SlackChannelAdapter(
        provider=SimpleNamespace(id="cp"),
        channel=SimpleNamespace(id="ch-1", external_id="C123"), inbox=None)
    a._conn = SimpleNamespace(app=SimpleNamespace(client=_FakeClient()))
    return a


@pytest.mark.asyncio
async def test_post_chat_media_uploads_into_thread():
    a = _adapter()
    res = await a.post_chat_media(
        [ImagePart(data=b"PNG", mime_type="image/png", ),
         DocumentPart(data=b"PDF", mime_type="application/pdf", filename="r.pdf")],
        thread_ts="t-9")
    client = a._conn.app.client
    assert len(client.uploads) == 2
    assert all(u["channel"] == "C123" for u in client.uploads)
    assert all(u["thread_ts"] == "t-9" for u in client.uploads)
    assert client.uploads[1]["filename"] == "r.pdf"
    assert res["sent"] == 2
