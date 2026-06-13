"""Discord inbound media: attachments become persisted artifact-backed parts."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from pydantic import SecretStr

from primer.bus.in_memory import InMemoryEventBus
from primer.channel.discord.adapter import DiscordChannelAdapter
from primer.int.artifact_storage import ArtifactBlob
from primer.model.agent import Agent
from primer.model.channel import (
    Channel, ChannelProvider, ChannelProviderType,
    ChatChannelAssociation, DiscordChannelProviderConfig,
)
from primer.model.chats import Chat, ChatMessage
from primer.model.provider import SqliteConfig
from primer.model.storage import OffsetPage
from primer.storage.q import Q
from primer.storage.sqlite import SqliteStorageProvider


class _MemArtifacts:
    """Minimal ArtifactStorage backend for tests."""

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


class _StubArtifactRegistry:
    """Stand-in for ArtifactStorageRegistry exposing get_default()."""

    def __init__(self, store):
        self._store = store

    async def get_default(self):
        return self._store


class _FakeAttachment:
    """discord.py Attachment look-alike: await .read(), .content_type, .filename."""

    def __init__(self, *, data: bytes, content_type, filename: str):
        self._data = data
        self.content_type = content_type
        self.filename = filename

    async def read(self) -> bytes:
        return self._data


def _png_bytes(w, h):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


async def _setup(tmp_path, *, with_artifacts=True):
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "r.sqlite"))
    await p.initialize()
    await p.get_storage(Agent).create(Agent(
        id="agent-x", description="X",
        model={"provider_id": "lp", "model_name": "m"}))
    cp = ChannelProvider(
        id="cp-1", provider=ChannelProviderType.DISCORD,
        config=DiscordChannelProviderConfig(bot_token=SecretStr("x" * 40)))
    ch = Channel(id="ch-1", provider_id="cp-1", external_id="9001")
    await p.get_storage(ChannelProvider).create(cp)
    await p.get_storage(Channel).create(ch)
    await p.get_storage(ChatChannelAssociation).create(ChatChannelAssociation(
        id="cca-1", channel_id="ch-1", default_agent_id="agent-x"))
    store = _MemArtifacts()
    adapter = DiscordChannelAdapter(
        provider=cp, channel=ch, inbox=None,
        storage_provider=p, event_bus=InMemoryEventBus(),
        artifact_registry=_StubArtifactRegistry(store) if with_artifacts else None)
    return p, adapter, store


async def _user_parts(p, chat_id):
    rows = (await p.get_storage(ChatMessage).find(
        Q(ChatMessage).where("chat_id", chat_id).build(),
        OffsetPage(offset=0, length=10))).items
    um = [r for r in rows if r.kind == "user_message"][0]
    return um.payload["parts"]


@pytest.mark.asyncio
async def test_image_attachment_persists_image_part(tmp_path: Path):
    p, adapter, store = await _setup(tmp_path)
    att = _FakeAttachment(
        data=_png_bytes(64, 64), content_type="image/png", filename="pic.png")
    chat = await adapter.handle_inbound_chat_message(
        thread_id=None, message_id="m-1", sender_name="Cara",
        text="look at this", attachments=[att])
    parts = await _user_parts(p, chat.id)
    text_parts = [pp for pp in parts if pp["type"] == "text"]
    image_parts = [pp for pp in parts if pp["type"] == "image"]
    assert text_parts[0]["text"] == "[Cara] look at this"
    assert len(image_parts) == 1
    aid = image_parts[0]["artifact_id"]
    assert aid in store.blobs


@pytest.mark.asyncio
async def test_document_attachment_persists_document_part(tmp_path: Path):
    p, adapter, store = await _setup(tmp_path)
    att = _FakeAttachment(
        data=b"%PDF-1.4 hello", content_type="application/pdf",
        filename="report.pdf")
    chat = await adapter.handle_inbound_chat_message(
        thread_id=None, message_id="m-2", sender_name="Cara",
        text="the doc", attachments=[att])
    parts = await _user_parts(p, chat.id)
    doc_parts = [pp for pp in parts if pp["type"] == "document"]
    assert len(doc_parts) == 1
    assert doc_parts[0]["filename"] == "report.pdf"
    assert doc_parts[0]["artifact_id"] in store.blobs


@pytest.mark.asyncio
async def test_oversized_attachment_skipped_text_still_lands(tmp_path: Path):
    p, adapter, store = await _setup(tmp_path)
    big = b"a" * (21 * 1024 * 1024)  # over the 20 MiB default cap
    att = _FakeAttachment(
        data=big, content_type="application/pdf", filename="huge.pdf")
    chat = await adapter.handle_inbound_chat_message(
        thread_id=None, message_id="m-3", sender_name="Cara",
        text="too big", attachments=[att])
    parts = await _user_parts(p, chat.id)
    media_parts = [pp for pp in parts if pp["type"] in ("image", "document")]
    text_parts = [pp for pp in parts if pp["type"] == "text"]
    assert media_parts == []
    assert text_parts[0]["text"].startswith("[Cara] too big")
    assert "skipped" in text_parts[0]["text"]
    assert store.blobs == {}


@pytest.mark.asyncio
async def test_no_artifact_registry_skips_media(tmp_path: Path):
    p, adapter, _store = await _setup(tmp_path, with_artifacts=False)
    att = _FakeAttachment(
        data=_png_bytes(32, 32), content_type="image/png", filename="pic.png")
    chat = await adapter.handle_inbound_chat_message(
        thread_id=None, message_id="m-4", sender_name="Cara",
        text="hi", attachments=[att])
    parts = await _user_parts(p, chat.id)
    media_parts = [pp for pp in parts if pp["type"] in ("image", "document")]
    text_parts = [pp for pp in parts if pp["type"] == "text"]
    assert media_parts == []
    assert text_parts[0]["text"] == "[Cara] hi"
