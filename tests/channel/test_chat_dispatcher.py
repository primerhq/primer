"""ChatChannelDispatcher: relay text + gate forwarding keyed on channel_binding."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from primer.channel.adapter import PromptEnvelope
from primer.channel.chat_dispatcher import (
    ChatChannelDispatcher,
    derive_chat_gate_envelope,
    derive_final_relay_media,
    derive_final_relay_text,
    parse_relay_event_key,
)


class _MemArtifacts:
    def __init__(self):
        from primer.int.artifact_storage import ArtifactBlob
        self._blob_cls = ArtifactBlob
        self.blobs = {}
        self._n = 0

    async def put(self, *, data, mime_type, filename=None):
        self._n += 1
        aid = f"artifact-{self._n}"
        self.blobs[aid] = self._blob_cls(
            data=data, mime_type=mime_type, filename=filename)
        return aid

    async def get(self, artifact_id):
        return self.blobs.get(artifact_id)
from primer.channel.null_adapter import NullChannelAdapter
from primer.model.chats import Chat, ChatChannelBinding, ChatMessage
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider


class _StubRegistry:
    """Warm registry: peek + get both return the adapter."""

    def __init__(self, adapter):
        self._adapter = adapter

    def peek_adapter(self, channel_id):
        return self._adapter

    async def get_adapter(self, channel_id):
        return self._adapter


class _ColdRegistry:
    """Out-of-proc worker registry: nothing warmed; never builds."""

    def peek_adapter(self, channel_id):
        return None

    async def get_adapter(self, channel_id):  # pragma: no cover - guard
        raise AssertionError("worker relay must not build an adapter")


class _RecordingBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, event_key, payload=None):
        self.published.append((event_key, payload or {}))


async def _provider(tmp_path):
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "r.sqlite"))
    await p.initialize()
    await p.get_storage(Chat).create(Chat(
        id="chat-1", agent_id="agent-x", created_at=datetime.now(timezone.utc),
        channel_binding=ChatChannelBinding(channel_id="ch-1", thread_external_id="t-9")))
    return p


@pytest.mark.asyncio
async def test_relay_text_posts_inform(tmp_path: Path):
    p = await _provider(tmp_path)
    adapter = NullChannelAdapter()
    d = ChatChannelDispatcher(storage_provider=p, registry=_StubRegistry(adapter))
    await d.relay_text(chat_id="chat-1", text="all done")
    assert len(adapter.posted) == 1
    env = adapter.posted[0]
    assert env.kind == "inform"
    assert env.prompt == "all done"
    assert env.session_id == "t-9"  # thread id carried as the routing key


@pytest.mark.asyncio
async def test_dispatch_gate_forwards_prompt_envelope(tmp_path: Path):
    p = await _provider(tmp_path)
    adapter = NullChannelAdapter()
    d = ChatChannelDispatcher(storage_provider=p, registry=_StubRegistry(adapter))
    env = PromptEnvelope(
        kind="ask_user", workspace_id="", session_id="chat-1",
        tool_call_id="tc-1", prompt="continue?", response_schema=None,
        choices=None, timeout_at_iso=None)
    posted = await d.dispatch_gate(chat_id="chat-1", envelope=env)
    assert posted is True
    assert adapter.posted[0].kind == "ask_user"


@pytest.mark.asyncio
async def test_unbound_chat_is_noop(tmp_path: Path):
    p = await _provider(tmp_path)
    await p.get_storage(Chat).create(Chat(
        id="chat-nobind", agent_id="agent-x",
        created_at=datetime.now(timezone.utc)))
    adapter = NullChannelAdapter()
    d = ChatChannelDispatcher(storage_provider=p, registry=_StubRegistry(adapter))
    await d.relay_text(chat_id="chat-nobind", text="x")
    assert adapter.posted == []


@pytest.mark.asyncio
async def test_cold_registry_publishes_relay_signal(tmp_path: Path):
    """Out-of-proc worker: no warm adapter -> publish a tiny bus signal
    instead of building a (second inbound) adapter."""
    p = await _provider(tmp_path)
    bus = _RecordingBus()
    d = ChatChannelDispatcher(
        storage_provider=p, registry=_ColdRegistry(), event_bus=bus)
    ok = await d.relay_text(chat_id="chat-1", text="all done")
    assert ok is True
    assert bus.published == [("chat:chat-1:relay", {"kind": "text"})]

    env = PromptEnvelope(
        kind="ask_user", workspace_id="", session_id="chat-1",
        tool_call_id="tc-1", prompt="continue?", response_schema=None,
        choices=None, timeout_at_iso=None)
    ok = await d.dispatch_gate(chat_id="chat-1", envelope=env)
    assert ok is True
    assert bus.published[-1] == ("chat:chat-1:relay", {"kind": "gate"})


@pytest.mark.asyncio
async def test_cold_registry_without_bus_drops(tmp_path: Path):
    """No warm adapter and no bus -> drop (never build inbound)."""
    p = await _provider(tmp_path)
    d = ChatChannelDispatcher(
        storage_provider=p, registry=_ColdRegistry(), event_bus=None)
    ok = await d.relay_text(chat_id="chat-1", text="all done")
    assert ok is False


async def _seed_done_turn(p, chat_id="chat-1"):
    """Two assistant_token deltas + a done row, the relayable final turn."""
    now = datetime.now(timezone.utc)
    msgs = p.get_storage(ChatMessage)
    rows = [
        ("user_message", {"content": "hi"}),
        ("assistant_token", {"delta": "all "}),
        ("assistant_token", {"delta": "done"}),
        ("done", {}),
    ]
    for seq, (kind, payload) in enumerate(rows, start=1):
        await msgs.create(ChatMessage(
            id=ChatMessage.make_id(chat_id, seq), chat_id=chat_id, seq=seq,
            kind=kind, payload=payload, created_at=now))


@pytest.mark.asyncio
async def test_out_of_proc_relay_round_trip(tmp_path: Path):
    """Worker (cold) publishes a signal; the inbound-owning forwarder
    re-derives text from storage and posts via its warm adapter."""
    p = await _provider(tmp_path)
    await _seed_done_turn(p)

    # Worker side: cold registry -> publishes a bare signal.
    bus = _RecordingBus()
    worker = ChatChannelDispatcher(
        storage_provider=p, registry=_ColdRegistry(), event_bus=bus)
    await worker.relay_text(chat_id="chat-1", text="all done")
    assert bus.published == [("chat:chat-1:relay", {"kind": "text"})]

    # Forwarder side: parse the key, re-derive text, post via warm adapter.
    key, payload = bus.published[0]
    cid = parse_relay_event_key(key)
    assert cid == "chat-1"
    text = await derive_final_relay_text(p, cid)
    assert text == "all done"
    adapter = NullChannelAdapter()
    relayer = ChatChannelDispatcher(
        storage_provider=p, registry=_StubRegistry(adapter), allow_build=True)
    await relayer.relay_text(chat_id=cid, text=text)
    assert adapter.posted[0].prompt == "all done"


@pytest.mark.asyncio
async def test_derive_chat_gate_envelope_from_pending(tmp_path: Path):
    p = await _provider(tmp_path)
    chat = await p.get_storage(Chat).get("chat-1")
    chat.pending_tool_call = {
        "tool_call_id": "tc9", "mode": "approval",
        "original_call": {"name": "fs__write_file"}}
    await p.get_storage(Chat).update(chat)
    env = await derive_chat_gate_envelope(p, "chat-1")
    assert env is not None
    assert env.kind == "tool_approval"
    assert env.tool_call_id == "tc9"
    assert "fs__write_file" in env.prompt


def test_parse_relay_event_key():
    assert parse_relay_event_key("chat:abc:relay") == "abc"
    assert parse_relay_event_key("chat::relay") is None
    assert parse_relay_event_key("chat:abc:tick") is None
    assert parse_relay_event_key("session:abc:relay") is None


@pytest.mark.asyncio
async def test_allow_build_uses_get_adapter(tmp_path: Path):
    """Inbound-owning forwarder builds via get_adapter and posts directly."""
    p = await _provider(tmp_path)
    adapter = NullChannelAdapter()

    class _BuildOnly:
        def peek_adapter(self, channel_id):
            return None  # nothing warm yet

        async def get_adapter(self, channel_id):
            return adapter

    d = ChatChannelDispatcher(
        storage_provider=p, registry=_BuildOnly(), allow_build=True)
    env = PromptEnvelope(
        kind="ask_user", workspace_id="", session_id="chat-1",
        tool_call_id="tc-1", prompt="continue?", response_schema=None,
        choices=None, timeout_at_iso=None)
    ok = await d.dispatch_gate(chat_id="chat-1", envelope=env)
    assert ok is True
    assert adapter.posted[0].kind == "ask_user"


async def _seed_media_turn(p, chat_id="chat-1"):
    """A done-terminated turn whose tool_result row carries an image media part
    in a forward-compat ``media`` list (the shape a media-returning tool will
    persist)."""
    from primer.model.chat import ImagePart
    now = datetime.now(timezone.utc)
    msgs = p.get_storage(ChatMessage)
    img = ImagePart(artifact_id="artifact-9", mime_type="image/png").model_dump(mode="json")
    rows = [
        ("user_message", {"content": "make a chart"}),
        ("tool_result", {"id": "tc1", "name": "chart", "result": "ok",
                          "media": [img]}),
        ("done", {}),
    ]
    for seq, (kind, payload) in enumerate(rows, start=1):
        await msgs.create(ChatMessage(
            id=ChatMessage.make_id(chat_id, seq), chat_id=chat_id, seq=seq,
            kind=kind, payload=payload, created_at=now))


@pytest.mark.asyncio
async def test_derive_final_relay_media(tmp_path: Path):
    p = await _provider(tmp_path)
    await _seed_media_turn(p)
    media = await derive_final_relay_media(p, "chat-1")
    assert len(media) == 1
    assert media[0].artifact_id == "artifact-9"


@pytest.mark.asyncio
async def test_relay_media_cold_publishes_signal(tmp_path: Path):
    from primer.model.chat import ImagePart
    p = await _provider(tmp_path)
    bus = _RecordingBus()
    d = ChatChannelDispatcher(
        storage_provider=p, registry=_ColdRegistry(), event_bus=bus)
    ok = await d.relay_media(
        chat_id="chat-1",
        parts=[ImagePart(artifact_id="artifact-9", mime_type="image/png")])
    assert ok is True
    assert bus.published == [("chat:chat-1:relay", {"kind": "media"})]


@pytest.mark.asyncio
async def test_relay_media_warm_posts_via_adapter(tmp_path: Path):
    from primer.model.chat import ImagePart

    class _MediaAdapter:
        def __init__(self):
            self.posted = []

        async def post_chat_media(self, parts, *, thread_ts=None):
            self.posted.append((parts, thread_ts))
            return {"sent": len(parts)}

    p = await _provider(tmp_path)
    adapter = _MediaAdapter()
    d = ChatChannelDispatcher(storage_provider=p, registry=_StubRegistry(adapter))
    ok = await d.relay_media(
        chat_id="chat-1",
        parts=[ImagePart(data=b"PNG", mime_type="image/png")])
    assert ok is True
    assert len(adapter.posted) == 1
    parts, thread_ts = adapter.posted[0]
    assert thread_ts == "t-9"
    assert parts[0].data == b"PNG"


@pytest.mark.asyncio
async def test_relay_media_hydrates_before_post(tmp_path: Path):
    from primer.int.artifact_storage import ArtifactBlob
    from primer.model.chat import ImagePart

    class _MediaAdapter:
        def __init__(self):
            self.posted = []

        async def post_chat_media(self, parts, *, thread_ts=None):
            self.posted.append(parts)

    class _ArtReg:
        def __init__(self, store):
            self._store = store

        async def get_default(self):
            return self._store

    p = await _provider(tmp_path)
    store = _MemArtifacts()
    aid = await store.put(data=b"IMGBYTES", mime_type="image/png")
    adapter = _MediaAdapter()
    d = ChatChannelDispatcher(
        storage_provider=p, registry=_StubRegistry(adapter),
        artifact_registry=_ArtReg(store))
    await d.relay_media(
        chat_id="chat-1",
        parts=[ImagePart(artifact_id=aid, mime_type="image/png")])
    posted = adapter.posted[0]
    assert posted[0].data == b"IMGBYTES"
    assert posted[0].artifact_id is None
