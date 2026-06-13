"""Tool-produced media (MCP image blocks) is captured into tool_result rows
and surfaced for the outbound channel relay."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from primer.channel.chat_dispatcher import derive_final_relay_media
from primer.channel.media import parts_from_tool_media
from primer.model.chat import ImagePart, ToolResultPart


class _MemArtifacts:
    def __init__(self):
        from primer.int.artifact_storage import ArtifactBlob
        self._cls = ArtifactBlob
        self.blobs = {}
        self._n = 0

    async def put(self, *, data, mime_type, filename=None):
        self._n += 1
        aid = f"artifact-{self._n}"
        self.blobs[aid] = self._cls(data=data, mime_type=mime_type, filename=filename)
        return aid

    async def get(self, artifact_id):
        return self.blobs.get(artifact_id)


def _png_b64():
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def test_tool_result_part_carries_media():
    rp = ToolResultPart(id="tc1", output="done",
                        media=[{"type": "image", "data": "x", "mimeType": "image/png"}])
    assert rp.media[0]["type"] == "image"


@pytest.mark.asyncio
async def test_parts_from_tool_media_stores_and_builds():
    store = _MemArtifacts()
    blocks = [
        {"type": "image", "data": _png_b64(), "mimeType": "image/png"},
        {"type": "text", "text": "ignored"},
        {"type": "resource",
         "resource": {"blob": base64.b64encode(b"PDFDATA").decode(),
                      "mimeType": "application/pdf"}},
    ]
    parts = await parts_from_tool_media(store, blocks)
    # image + resource(blob) captured; text ignored.
    assert len(parts) == 2
    assert isinstance(parts[0], ImagePart)
    assert parts[0].artifact_id in store.blobs
    assert parts[0].data is None


@pytest.mark.asyncio
async def test_parts_from_tool_media_skips_bad_base64():
    store = _MemArtifacts()
    parts = await parts_from_tool_media(
        store, [{"type": "image", "data": "!!notb64!!", "mimeType": "image/png"}])
    assert parts == []


@pytest.mark.asyncio
async def test_runner_persists_tool_media_into_row(tmp_path: Path):
    """End-to-end at the persistence layer: a tool result with media -> a
    tool_result row whose payload['media'] is a media part, picked up by
    derive_final_relay_media."""
    from datetime import datetime, timezone
    from primer.model.chats import Chat, ChatMessage
    from primer.model.provider import SqliteConfig
    from primer.storage.sqlite import SqliteStorageProvider

    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "r.sqlite"))
    await p.initialize()
    chat = Chat(id="chat-1", agent_id="ag",
                created_at=datetime.now(timezone.utc))
    await p.get_storage(Chat).create(chat)

    # Build a minimal runner with only the artifact store + storages it needs
    # for _tool_media_parts + _append. Use a real store.
    store = _MemArtifacts()
    from primer.chat.executor import ChatTurnRunner
    runner = ChatTurnRunner.__new__(ChatTurnRunner)
    runner._artifacts = store

    rp = ToolResultPart(
        id="tc1", output="here is your chart",
        media=[{"type": "image", "data": _png_b64(), "mimeType": "image/png"}])
    parts = await runner._tool_media_parts(rp)
    assert len(parts) == 1

    # Persist a tool_result row + done, then derive.
    now = datetime.now(timezone.utc)
    msgs = p.get_storage(ChatMessage)
    await msgs.create(ChatMessage(
        id=ChatMessage.make_id("chat-1", 1), chat_id="chat-1", seq=1,
        kind="user_message", payload={"content": "chart please"}, created_at=now))
    await msgs.create(ChatMessage(
        id=ChatMessage.make_id("chat-1", 2), chat_id="chat-1", seq=2,
        kind="tool_result",
        payload={"id": "tc1", "name": "chart", "result": "ok",
                 "media": [p0.model_dump(mode="json") for p0 in parts]},
        created_at=now))
    await msgs.create(ChatMessage(
        id=ChatMessage.make_id("chat-1", 3), chat_id="chat-1", seq=3,
        kind="done", payload={}, created_at=now))

    media = await derive_final_relay_media(p, "chat-1")
    assert len(media) == 1
    assert isinstance(media[0], ImagePart)
    assert media[0].artifact_id in store.blobs
