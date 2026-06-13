"""Telegram inbound media: photo/document/oversized/no-artifacts paths.

A media message carries its user text in ``msg.caption`` (``msg.text`` is
None). The adapter downloads each attachment's bytes via the bot file API,
stores them through ``store_inbound_media`` (artifact-backed), and routes the
turn through the chat router so the persisted ``user_message`` parts include
the media part plus the caption as the leading text part.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from pydantic import SecretStr

from primer.bus.in_memory import InMemoryEventBus
from primer.channel.media import MediaConfig
from primer.channel.telegram.adapter import TelegramChannelAdapter
from primer.int.artifact_storage import ArtifactBlob
from primer.model.agent import Agent
from primer.model.channel import (
    Channel, ChannelProvider, ChannelProviderType,
    TelegramChannelConfig, TelegramChannelProviderConfig,
)
from primer.model.chats import Chat, ChatMessage
from primer.model.storage import OffsetPage
from primer.storage.q import Q
from primer.storage.sqlite import SqliteStorageProvider
from primer.model.provider import SqliteConfig


# --- fakes -------------------------------------------------------------------


class _MemArtifacts:
    """Minimal ArtifactStorage stand-in."""

    def __init__(self):
        self.blobs: dict[str, ArtifactBlob] = {}
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


class _FakeRegistry:
    def __init__(self, store):
        self._store = store

    async def get_default(self):
        return self._store


class _FakeFile:
    def __init__(self, data: bytes):
        self._data = data

    async def download_as_bytearray(self):
        return bytearray(self._data)


class _FakeBot:
    def __init__(self, files: dict[str, bytes]):
        self._files = files

    async def get_file(self, file_id):
        return _FakeFile(self._files[file_id])


class _FakeApp:
    def __init__(self, bot):
        self.bot = bot


# fake PTB message attachment shapes -----------------------------------------


class _FakePhotoSize:
    def __init__(self, file_id):
        self.file_id = file_id


class _FakeDocument:
    def __init__(self, file_id, mime_type, file_name):
        self.file_id = file_id
        self.mime_type = mime_type
        self.file_name = file_name


class _FakeMessage:
    """Minimal PTB-message-like object for the extractor."""

    def __init__(
        self, *, caption=None, photo=None, document=None,
        audio=None, voice=None, video=None,
    ):
        self.caption = caption
        self.photo = photo or []
        self.document = document
        self.audio = audio
        self.voice = voice
        self.video = video


# --- helpers -----------------------------------------------------------------


def _png_bytes(w=64, h=64):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 120, 240)).save(buf, format="PNG")
    return buf.getvalue()


async def _setup(tmp_path, *, with_artifacts=True, files=None):
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "r.sqlite"))
    await p.initialize()
    await p.get_storage(Agent).create(Agent(
        id="agent-x", description="X",
        model={"provider_id": "lp", "model_name": "m"}))
    cp = ChannelProvider(
        id="cp-1", provider=ChannelProviderType.TELEGRAM,
        config=TelegramChannelProviderConfig(
            bot_token=SecretStr("123456:ABCDEFGHIJKLMNOP")))
    ch = Channel(
        id="ch-1", provider_id="cp-1", provider=ChannelProviderType.TELEGRAM,
        external_id="555",
        config=TelegramChannelConfig(chats={
            "enabled": True, "default_agent": "agent-x"}))
    await p.get_storage(ChannelProvider).create(cp)
    await p.get_storage(Channel).create(ch)

    store = _MemArtifacts()
    registry = _FakeRegistry(store) if with_artifacts else None
    adapter = TelegramChannelAdapter(
        provider=cp, channel=ch, inbox=None,
        storage_provider=p, event_bus=InMemoryEventBus(),
        artifact_registry=registry)
    adapter._app = _FakeApp(_FakeBot(files or {}))
    return p, adapter, store


async def _user_message_parts(p, chat_id):
    rows = (await p.get_storage(ChatMessage).find(
        Q(ChatMessage).where("chat_id", chat_id).build(),
        OffsetPage(offset=0, length=20))).items
    um = [r for r in rows if r.kind == "user_message"][0]
    return um.payload["parts"]


# --- tests -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_photo_message_persists_image_part_with_artifact(tmp_path: Path):
    png = _png_bytes()
    p, adapter, store = await _setup(
        tmp_path, files={"photo-hi": png})
    msg = _FakeMessage(
        caption="look at this",
        photo=[_FakePhotoSize("photo-lo"), _FakePhotoSize("photo-hi")])
    # only the highest-res file is registered, proving the LAST is taken.
    await adapter.handle_inbound_chat_media(sender_name="Alice", msg=msg)

    chats = (await p.get_storage(Chat).list(OffsetPage(offset=0, length=10))).items
    parts = await _user_message_parts(p, chats[0].id)
    text_parts = [pt for pt in parts if pt["type"] == "text"]
    image_parts = [pt for pt in parts if pt["type"] == "image"]
    assert text_parts and text_parts[0]["text"] == "[Alice] look at this"
    assert len(image_parts) == 1
    assert image_parts[0]["artifact_id"] in store.blobs


@pytest.mark.asyncio
async def test_document_message_preserves_filename(tmp_path: Path):
    p, adapter, store = await _setup(
        tmp_path, files={"doc-1": b"%PDF-1.4 hello"})
    msg = _FakeMessage(
        caption=None,
        document=_FakeDocument("doc-1", "application/pdf", "report.pdf"))
    await adapter.handle_inbound_chat_media(sender_name="Bob", msg=msg)

    chats = (await p.get_storage(Chat).list(OffsetPage(offset=0, length=10))).items
    parts = await _user_message_parts(p, chats[0].id)
    doc_parts = [pt for pt in parts if pt["type"] == "document"]
    assert len(doc_parts) == 1
    assert doc_parts[0]["artifact_id"] in store.blobs
    assert doc_parts[0]["filename"] == "report.pdf"


@pytest.mark.asyncio
async def test_oversized_attachment_skipped_turn_lands_as_text(tmp_path: Path):
    png = _png_bytes(64, 64)
    p, adapter, store = await _setup(tmp_path, files={"big": png})
    # Tiny cap forces MediaTooLarge inside store_inbound_media.
    adapter._media_config = MediaConfig(max_bytes=4)
    msg = _FakeMessage(
        caption="too big",
        photo=[_FakePhotoSize("big")])
    await adapter.handle_inbound_chat_media(sender_name="Carol", msg=msg)

    chats = (await p.get_storage(Chat).list(OffsetPage(offset=0, length=10))).items
    parts = await _user_message_parts(p, chats[0].id)
    assert not [pt for pt in parts if pt["type"] == "image"]
    text_parts = [pt for pt in parts if pt["type"] == "text"]
    assert text_parts
    assert "too big" in text_parts[0]["text"]
    assert "skipped" in text_parts[0]["text"].lower()
    assert not store.blobs


@pytest.mark.asyncio
async def test_no_artifact_registry_skips_media_text_only(tmp_path: Path):
    p, adapter, store = await _setup(
        tmp_path, with_artifacts=False, files={"photo-hi": _png_bytes()})
    msg = _FakeMessage(
        caption="hi there",
        photo=[_FakePhotoSize("photo-hi")])
    await adapter.handle_inbound_chat_media(sender_name="Dan", msg=msg)

    chats = (await p.get_storage(Chat).list(OffsetPage(offset=0, length=10))).items
    parts = await _user_message_parts(p, chats[0].id)
    assert not [pt for pt in parts if pt["type"] == "image"]
    text_parts = [pt for pt in parts if pt["type"] == "text"]
    assert text_parts and text_parts[0]["text"] == "[Dan] hi there"
