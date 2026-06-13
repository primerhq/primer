"""Inbound Slack media: event["files"] are downloaded, stored as artifacts,
and delivered as media parts on the chat turn.

The httpx download is monkeypatched to canned bytes so no network is used.
A fake artifact store (mirroring tests/channel/test_media.py) backs storage;
a real SqliteStorageProvider backs the chat rows (mirroring
tests/channel/slack/test_slack_relay_thread.py).
"""

from __future__ import annotations

import io
import os

import pytest

from primer.channel.slack.adapter import SlackChannelAdapter
from primer.int.artifact_storage import ArtifactBlob
from primer.model.channel import (
    Channel, ChannelProvider, ChannelProviderType,
    ChatChannelAssociation, SlackChannelProviderConfig,
)
from primer.model.agent import Agent
from primer.model.chats import Chat, ChatMessage
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider


# ---- fakes -----------------------------------------------------------------


class _MemArtifacts:
    """An ArtifactStorage backend that keeps blobs in a dict."""

    def __init__(self):
        self.blobs = {}
        self._n = 0

    async def initialize(self): ...
    async def aclose(self): ...

    async def put(self, *, data, mime_type, filename=None):
        self._n += 1
        aid = f"artifact-{self._n}"
        self.blobs[aid] = ArtifactBlob(
            data=data, mime_type=mime_type, filename=filename)
        return aid

    async def get(self, artifact_id):
        return self.blobs.get(artifact_id)

    async def delete(self, artifact_id):
        self.blobs.pop(artifact_id, None)


class _Registry:
    """Stand-in for ArtifactStorageRegistry: get_default() yields the store."""

    def __init__(self, store):
        self._store = store

    async def get_default(self):
        return self._store


def _png_bytes(w=64, h=64):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 120, 240)).save(buf, format="PNG")
    return buf.getvalue()


class _FakeResponse:
    def __init__(self, content: bytes, status_code: int = 200):
        self.content = content
        self.status_code = status_code


class _FakeAsyncClient:
    """Drop-in for httpx.AsyncClient whose GET returns canned bytes keyed by
    URL. A url mapped to a non-200 simulates a download failure."""

    _by_url: dict = {}

    def __init__(self, *a, **k):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None):
        type(self).last_headers = headers
        resp = self._by_url.get(url)
        if resp is None:
            return _FakeResponse(b"", status_code=404)
        return resp


# ---- harness ---------------------------------------------------------------


def _make_provider() -> ChannelProvider:
    # Token comes from env when present (never a hardcoded real secret); the
    # placeholder only has to satisfy the 'xoxb-'/'xapp-' format validators.
    bot = os.environ.get("SLACK_BOT_TOKEN", "xoxb-test-bot-token")
    app = os.environ.get("SLACK_APP_TOKEN", "xapp-test-app-token")
    return ChannelProvider(
        id="prov-1",
        provider=ChannelProviderType.SLACK,
        config=SlackChannelProviderConfig(app_token=app, bot_token=bot),
    )


def _make_channel() -> Channel:
    return Channel(
        id="ch-1", provider_id="prov-1", external_id="C123",
        label="primer-testing",
    )


async def _make_sp(tmp_path) -> SqliteStorageProvider:
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "media.sqlite"))
    await sp.initialize()
    # Channel ch-1 needs an association + a real agent so the router can
    # resolve-or-create a chat for the inbound message.
    await sp.get_storage(Agent).create(Agent(
        id="agent-x", description="Xavier",
        model={"provider_id": "lp", "model_name": "m"}))
    await sp.get_storage(ChatChannelAssociation).create(ChatChannelAssociation(
        id="cca-1", channel_id="ch-1", default_agent_id="agent-x"))
    return sp


def _make_adapter(sp, store) -> SlackChannelAdapter:
    return SlackChannelAdapter(
        provider=_make_provider(), channel=_make_channel(), inbox=None,
        storage_provider=sp, event_bus=None, claim_engine=None,
        artifact_registry=_Registry(store) if store is not None else None,
    )


async def _user_parts(sp, chat_id) -> list[dict]:
    # A fresh thread-chat's first inbound user_message lands at seq 1.
    chat = await sp.get_storage(Chat).get(chat_id)
    assert chat is not None and chat.last_seq >= 1, "no user_message persisted"
    row = await sp.get_storage(ChatMessage).get(
        ChatMessage.make_id(chat_id, chat.last_seq))
    assert row is not None and row.kind == "user_message"
    return row.payload.get("parts", [])


@pytest.fixture(autouse=True)
def _patch_httpx(monkeypatch):
    import primer.channel.slack.adapter as adapter_mod
    monkeypatch.setattr(adapter_mod.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient._by_url = {}
    _FakeAsyncClient.last_headers = None
    yield


# ---- tests -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_image_file_becomes_image_part_with_artifact(tmp_path):
    sp = await _make_sp(tmp_path)
    store = _MemArtifacts()
    adapter = _make_adapter(sp, store)
    img = _png_bytes()
    _FakeAsyncClient._by_url["https://files.slack/img"] = _FakeResponse(img)

    chat = await adapter.handle_inbound_chat_message(
        thread_ts=None, message_ts="1700.1", sender_name="U1",
        text="look at this",
        files=[{
            "url_private_download": "https://files.slack/img",
            "mimetype": "image/png", "name": "shot.png",
        }],
    )

    parts = await _user_parts(sp, chat.id)
    text_parts = [p for p in parts if p.get("type") == "text"]
    image_parts = [p for p in parts if p.get("type") == "image"]
    assert text_parts and "look at this" in text_parts[0]["text"]
    assert len(image_parts) == 1
    aid = image_parts[0]["artifact_id"]
    assert aid and aid in store.blobs
    assert image_parts[0].get("data") is None
    # The bot token was sent as a bearer header on the download.
    bot = _make_provider().config.bot_token.get_secret_value()
    assert _FakeAsyncClient.last_headers == {"Authorization": f"Bearer {bot}"}


@pytest.mark.asyncio
async def test_document_file_becomes_document_part_with_filename(tmp_path):
    sp = await _make_sp(tmp_path)
    store = _MemArtifacts()
    adapter = _make_adapter(sp, store)
    pdf = b"%PDF-1.4 fake document bytes"
    _FakeAsyncClient._by_url["https://files.slack/doc"] = _FakeResponse(pdf)

    chat = await adapter.handle_inbound_chat_message(
        thread_ts=None, message_ts="1700.2", sender_name="U1", text="",
        files=[{
            "url_private_download": "https://files.slack/doc",
            "mimetype": "application/pdf", "name": "report.pdf",
        }],
    )

    parts = await _user_parts(sp, chat.id)
    doc_parts = [p for p in parts if p.get("type") == "document"]
    assert len(doc_parts) == 1
    assert doc_parts[0]["filename"] == "report.pdf"
    aid = doc_parts[0]["artifact_id"]
    assert aid in store.blobs
    assert store.blobs[aid].data == pdf


@pytest.mark.asyncio
async def test_oversized_file_skipped_turn_lands_as_text(tmp_path):
    sp = await _make_sp(tmp_path)
    store = _MemArtifacts()
    adapter = _make_adapter(sp, store)
    # 21 MiB exceeds the 20 MiB default cap -> MediaTooLarge -> skipped.
    big = b"x" * (21 * 1024 * 1024)
    _FakeAsyncClient._by_url["https://files.slack/big"] = _FakeResponse(big)

    chat = await adapter.handle_inbound_chat_message(
        thread_ts=None, message_ts="1700.3", sender_name="U1", text="huge one",
        files=[{
            "url_private_download": "https://files.slack/big",
            "mimetype": "application/pdf", "name": "huge.pdf",
        }],
    )

    parts = await _user_parts(sp, chat.id)
    assert not [p for p in parts if p.get("type") == "document"]
    text_parts = [p for p in parts if p.get("type") == "text"]
    assert text_parts
    joined = text_parts[0]["text"]
    assert "huge one" in joined
    assert "attachment skipped" in joined
    assert store.blobs == {}


@pytest.mark.asyncio
async def test_no_artifact_registry_skips_media_gracefully(tmp_path):
    sp = await _make_sp(tmp_path)
    adapter = _make_adapter(sp, store=None)  # artifact_registry None
    _FakeAsyncClient._by_url["https://files.slack/img"] = _FakeResponse(_png_bytes())

    chat = await adapter.handle_inbound_chat_message(
        thread_ts=None, message_ts="1700.4", sender_name="U1", text="hi there",
        files=[{
            "url_private_download": "https://files.slack/img",
            "mimetype": "image/png", "name": "shot.png",
        }],
    )

    parts = await _user_parts(sp, chat.id)
    assert not [p for p in parts if p.get("type") == "image"]
    text_parts = [p for p in parts if p.get("type") == "text"]
    assert text_parts and "hi there" in text_parts[0]["text"]


@pytest.mark.asyncio
async def test_download_failure_skips_file_without_raising(tmp_path):
    sp = await _make_sp(tmp_path)
    store = _MemArtifacts()
    adapter = _make_adapter(sp, store)
    # Mapped to a 500 -> download failure -> skip, no raise.
    _FakeAsyncClient._by_url["https://files.slack/bad"] = _FakeResponse(
        b"", status_code=500)

    chat = await adapter.handle_inbound_chat_message(
        thread_ts=None, message_ts="1700.5", sender_name="U1", text="should survive",
        files=[{
            "url_private_download": "https://files.slack/bad",
            "mimetype": "image/png", "name": "shot.png",
        }],
    )

    parts = await _user_parts(sp, chat.id)
    assert not [p for p in parts if p.get("type") == "image"]
    text_parts = [p for p in parts if p.get("type") == "text"]
    assert text_parts and "should survive" in text_parts[0]["text"]
    assert store.blobs == {}
