"""S6 P4: routed media lands in the session's workspace before the steer.

Spec: docs/superpowers/ux-revamp/10-s6-design.md section 6.
"""

from __future__ import annotations

from datetime import UTC, datetime

import primer.channel.event_dispatch as ed
from primer.channel.correlation import CorrelationStore
from primer.channel.event_dispatch import ChannelEventRouter
from primer.model.channel import Channel, ChannelProviderType, TelegramChannelConfig
from primer.model.channel_event import (
    ChannelEvent,
    EventSender,
    NormalizedEventType,
)
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.steer_delivery import SteerDelivery
from primer.trigger.subscribers import DispatchDeps
from tests.conftest import _FakeStorageProvider


class _Part:
    def __init__(self, mime_type, filename, data) -> None:
        self.mime_type = mime_type
        self.filename = filename
        self.data = data
        self.artifact_id = None


class _FakeWorkspace:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    async def write_file(self, path: str, content: bytes) -> None:
        self.files[path] = content


class _FakeRegistry:
    def __init__(self, workspace) -> None:
        self.workspace = workspace

    async def get_workspace(self, workspace_id: str):
        return self.workspace


def _channel() -> Channel:
    return Channel(
        id="ch-1", provider_id="cp-1",
        provider=ChannelProviderType.TELEGRAM, external_id="777",
        config=TelegramChannelConfig(),
    )


def _event(text: str) -> ChannelEvent:
    return ChannelEvent(
        provider=ChannelProviderType.TELEGRAM, provider_id="cp-1",
        event_id="ev-m", type=NormalizedEventType.MESSAGE_POSTED,
        occurred_at=datetime.now(UTC), channel_id="ch-1", surface="thread",
        thread_anchor="thr-1", sender=EventSender(external_id="u1"), text=text,
    )


async def _route(monkeypatch, parts, *, fire_id="fire-1"):
    sp = _FakeStorageProvider()
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id="s1", workspace_id="w1",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.WAITING, created_at=datetime.now(UTC),
    ))
    await CorrelationStore(sp).upsert_thread_session(
        channel_id="ch-1", anchor="thr-1", workspace_id="w1", session_id="s1",
    )
    steers: list[str] = []

    async def _fake_deliver(**kw):
        steers.append(kw["text"])
        return SteerDelivery(outcome="queued", session_id="s1")

    monkeypatch.setattr(ed, "deliver_steer", _fake_deliver)
    workspace = _FakeWorkspace()
    router = ChannelEventRouter(
        storage_provider=sp,
        correlation_store=CorrelationStore(sp),
        fire_deps=DispatchDeps(
            storage_provider=sp, claim_engine=None, scheduler=None,
            workspace_registry=_FakeRegistry(workspace),
        ),
        event_bus=None,
    )
    await router.route_event(
        event=_event("look at this"), channel=_channel(), media_parts=parts,
    )
    return workspace, steers


async def test_attachment_lands_and_the_steer_references_it(monkeypatch):
    workspace, steers = await _route(
        monkeypatch, [_Part("image/png", "shot.png", b"PNG")],
    )
    assert any(p.endswith("/shot.png") for p in workspace.files)
    assert "look at this" in steers[0]
    assert "shot.png" in steers[0]


async def test_voice_note_without_stt_attaches_with_a_note(monkeypatch):
    from primer.channel import media_in

    async def _no_stt(*_args, **_kwargs):
        return None

    monkeypatch.setattr(media_in, "resolve_active_stt", _no_stt)
    _workspace, steers = await _route(
        monkeypatch, [_Part("audio/ogg", "note.ogg", b"OggS")],
    )
    assert media_in.NO_STT_NOTE in steers[0]


async def test_no_media_leaves_the_text_alone(monkeypatch):
    _workspace, steers = await _route(monkeypatch, None)
    assert steers == ["look at this"]
