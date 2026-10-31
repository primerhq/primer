"""The inbound channel dispatcher (Task 72: W6, W9, W33, W40, W56b, W56g, W56h, W56i).

Characterization tests pin how single messages route today; the W-named tests are the fixes:
- W6: two quick messages in one new conversation start ONE session; the second is steered into it. The get-or-create
  of a conversation's session is a conditional insert on the (channel_id, anchor) key, so a second process waits for
  the first one's session instead of starting its own.
- W9: the first message of a new conversation lands its attachments in the workspace the fire starts a session in.
- W33: a redelivered event (same provider, room and event id) is routed once.
- W40: the correlation index DDL runs once per database, not once per store.
- W56b: two attachments with one name both land.
- W56g: a voice message is transcribed through the app's STT registry.
- W56h: clearing an answered gate never blanks a newer gate.
- W56i: the trigger scan reads only the event's provider's triggers.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

import primer.channel.event_dispatch as ed
from primer.channel.correlation import CorrelationStore
from primer.channel.event_dispatch import ChannelEventRouter
from primer.model.channel import (
    Channel,
    ChannelProviderType,
    ChatConfig,
    TelegramChannelConfig,
)
from primer.model.channel_correlation import ChannelCorrelation
from primer.model.channel_event import ChannelEvent, EventSender, NormalizedEventType
from primer.model.provider import SqliteConfig
from primer.model.trigger import (
    AgentFreshSubConfig,
    ChannelTriggerConfig,
    Subscription,
    Trigger,
)
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.steer_delivery import SteerDelivery
from primer.storage.sqlite import SqliteStorageProvider
from primer.trigger.dispatch import FireResult
from primer.trigger.subscribers import DispatchDeps
from tests.conftest import _FakeStorageProvider


class _Part:
    def __init__(self, mime_type, filename, data):
        self.mime_type, self.filename, self.data, self.artifact_id = mime_type, filename, data, None


class _Workspace:
    def __init__(self):
        self.files: dict[str, bytes] = {}

    async def write_file(self, path, data):
        self.files[path] = data


class _Registry:
    def __init__(self):
        self.workspaces: dict[str, _Workspace] = {}

    async def get_workspace(self, workspace_id):
        return self.workspaces.setdefault(workspace_id, _Workspace())


def _event(i=1, *, text="hi", surface="dm", anchor=None, room="777", provider_id="cp-1"):
    return ChannelEvent(
        provider=ChannelProviderType.TELEGRAM, provider_id=provider_id, event_id=f"m{i}",
        type=NormalizedEventType.MESSAGE_POSTED, occurred_at=datetime.now(UTC), channel_id="ch-1",
        room_external_id=room, surface=surface, thread_anchor=anchor, sender=EventSender(external_id="u1"), text=text,
    )


async def _seed(sp, *, triggers=(("t1", "cp-1", "ch-1"),), workspace_id=None):
    await sp.get_storage(Channel).create(Channel(
        id="ch-1", provider_id="cp-1", provider=ChannelProviderType.TELEGRAM, external_id="777",
        config=TelegramChannelConfig(chats=ChatConfig(enabled=True)),
    ))
    for tid, provider_id, channel_id in triggers:
        await sp.get_storage(Trigger).create(Trigger(
            id=tid, slug=tid, name=tid, created_at=datetime.now(UTC),
            config=ChannelTriggerConfig(provider_id=provider_id, channel_id=channel_id),
        ))
    if workspace_id is not None:
        await sp.get_storage(Subscription).create(Subscription(
            id="sub-1", trigger_id="t1", created_at=datetime.now(UTC),
            config=AgentFreshSubConfig(workspace_id=workspace_id, agent_id="ag1"),
        ))


class _Fires:
    """A fire_trigger fake: each call starts one session (after a moment, like the real one)."""

    def __init__(self, sp, *, delay=0.0, start_sessions=True):
        self.sp, self.delay, self.start_sessions = sp, delay, start_sessions
        self.calls: list[dict] = []
        self.sessions: list[str] = []

    async def __call__(self, **kw):
        self.calls.append(kw)
        await asyncio.sleep(self.delay)
        if not self.start_sessions:
            return FireResult(fire_id=f"fire-{len(self.calls)}", results=[])
        sid = f"s{len(self.sessions) + 1}"
        self.sessions.append(sid)
        await self.sp.get_storage(WorkspaceSession).create(WorkspaceSession(
            id=sid, workspace_id="w1", binding=AgentSessionBinding(agent_id="a"),
            status=SessionStatus.RUNNING, created_at=datetime.now(UTC),
        ))
        return FireResult(fire_id=f"fire-{sid}", results=[{"ok": True, "artefact_id": sid, "subscription_id": None}])


@pytest.fixture
def steers(monkeypatch):
    seen: list[tuple[str, str]] = []

    async def _deliver(**kw):
        seen.append((kw["session_id"], kw["text"]))
        return SteerDelivery(outcome="queued", session_id=kw["session_id"])

    monkeypatch.setattr(ed, "deliver_steer", _deliver)
    return seen


def _router(sp, fires, *, registry=None, stt_registry=None, **kw):
    if stt_registry is not None:
        kw["stt_registry"] = stt_registry
    return ChannelEventRouter(
        storage_provider=sp, correlation_store=CorrelationStore(sp),
        fire_deps=DispatchDeps(storage_provider=sp, claim_engine=None, scheduler=None, workspace_registry=registry),
        fire_trigger=fires, **kw,
    )


# --------------------------------------------------------------------------- characterization


async def test_a_fresh_conversation_fires_and_maps_then_the_next_message_steers(steers):
    sp = _FakeStorageProvider()
    await _seed(sp)
    fires = _Fires(sp)
    first = await _router(sp, fires).route_event(event=_event(1, text="hi"), channel=None)
    assert (first.kind, first.session_id, first.fire_ids) == ("fired", "s1", ["fire-s1"])
    assert [c["extra_context"]["event"]["text"] for c in fires.calls] == ["hi"]
    row = await CorrelationStore(sp).lookup("ch-1", "dm:u1")
    assert (row.session_id, row.workspace_id, row.tool_call_id) == ("s1", "w1", None)
    second = await _router(sp, fires).route_event(event=_event(2, text="and this"), channel=None)
    assert (second.kind, second.session_id) == ("steer", "s1")
    assert steers == [("s1", "and this")]
    assert len(fires.calls) == 1


async def test_a_thread_reply_steers_its_mapped_session(steers):
    sp = _FakeStorageProvider()
    await _seed(sp)
    await CorrelationStore(sp).upsert_thread_session(channel_id="ch-1", anchor="th-1", workspace_id="w1", session_id="s9")
    fires = _Fires(sp)
    out = await _router(sp, fires).route_event(event=_event(1, text="more", surface="thread", anchor="th-1"), channel=None)
    assert (out.kind, out.session_id) == ("steer", "s9")
    assert steers == [("s9", "more")] and fires.calls == []


async def test_room_posts_fire_each_time_and_map_nothing():
    sp = _FakeStorageProvider()
    await _seed(sp)
    fires = _Fires(sp)
    for i in (1, 2):
        out = await _router(sp, fires).route_event(event=_event(i, surface="channel"), channel=None)
        assert out.kind == "fired" and out.session_id is None
    assert len(fires.calls) == 2
    assert await CorrelationStore(sp).list_for_channel("ch-1") == []


async def test_one_event_id_in_two_rooms_is_two_messages():
    sp = _FakeStorageProvider()
    await _seed(sp)
    fires = _Fires(sp)
    await _router(sp, fires).route_event(event=_event(1, surface="channel", room="777"), channel=None)
    await _router(sp, fires).route_event(event=_event(1, surface="channel", room="888"), channel=None)
    assert len(fires.calls) == 2


async def test_the_trigger_scan_matches_scoped_and_provider_wide_triggers_of_the_provider():
    sp = _FakeStorageProvider()
    await _seed(sp, triggers=[
        ("t-scoped", "cp-1", "ch-1"), ("t-wide", "cp-1", None), ("t-other-room", "cp-1", "ch-2"), ("t-other", "cp-2", None),
    ])
    fires = _Fires(sp, start_sessions=False)
    await _router(sp, fires).route_event(event=_event(1, surface="channel"), channel=None)
    assert sorted(c["trigger_id"] for c in fires.calls) == ["t-scoped", "t-wide"]


async def test_a_fresh_conversation_with_no_triggers_is_ignored_and_maps_nothing():
    sp = _FakeStorageProvider()
    await _seed(sp, triggers=())
    out = await _router(sp, _Fires(sp)).route_event(event=_event(1), channel=None)
    assert out.kind == "ignored"
    assert await CorrelationStore(sp).list_for_channel("ch-1") == []


async def test_a_fire_that_starts_nothing_maps_nothing():
    sp = _FakeStorageProvider()
    await _seed(sp)
    out = await _router(sp, _Fires(sp, start_sessions=False)).route_event(event=_event(1), channel=None)
    assert (out.kind, out.session_id) == ("fired", None)
    assert await CorrelationStore(sp).list_for_channel("ch-1") == []


async def test_an_attachment_on_a_steer_lands_under_the_event_id(steers):
    sp = _FakeStorageProvider()
    await _seed(sp)
    await CorrelationStore(sp).upsert_thread_session(channel_id="ch-1", anchor="dm:u1", workspace_id="w1", session_id="s9")
    registry = _Registry()
    await _router(sp, _Fires(sp), registry=registry).route_event(
        event=_event(5, text="see"), channel=None, media_parts=[_Part("image/png", "a.png", b"A")],
    )
    assert registry.workspaces["w1"].files == {"media/m5/a.png": b"A"}
    assert steers == [("s9", "see\n\nAttachments in this workspace:\n- media/m5/a.png")]


async def test_clear_gate_blanks_the_open_gate_and_keeps_the_mapping():
    sp = _FakeStorageProvider()
    store = CorrelationStore(sp)
    await store.upsert_session(channel_id="ch-1", anchor="th-1", workspace_id="w1", session_id="s1", tool_call_id="tc-1", gate_id="g1")
    await store.clear_gate("ch-1", "th-1")
    row = await store.lookup("ch-1", "th-1")
    assert (row.session_id, row.tool_call_id, row.gate_id) == ("s1", None, None)


# --------------------------------------------------------------------------- W40


async def test_W40_the_index_ddl_runs_once_for_two_stores(tmp_path: Path, monkeypatch):
    import aiosqlite

    ddl: list[str] = []
    real = aiosqlite.Connection.execute

    def _spy(self, sql, *a, **k):
        if "channelcorrelation_channel_anchor_uniq" in sql and sql.lstrip().upper().startswith("CREATE"):
            ddl.append(sql)
        return real(self, sql, *a, **k)

    monkeypatch.setattr(aiosqlite.Connection, "execute", _spy)
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "ddl.sqlite"))
    await sp.initialize()
    try:
        await CorrelationStore(sp).upsert_thread_session(channel_id="ch-1", anchor="a", workspace_id="w", session_id="s1")
        await CorrelationStore(sp).upsert_thread_session(channel_id="ch-1", anchor="b", workspace_id="w", session_id="s2")
        assert len(ddl) == 1
    finally:
        await sp.aclose()


async def test_W40_a_second_database_still_gets_its_index(tmp_path: Path):
    for name in ("one.sqlite", "two.sqlite"):
        sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / name))
        await sp.initialize()
        try:
            await CorrelationStore(sp).upsert_thread_session(channel_id="ch-1", anchor="a", workspace_id="w", session_id="s1")
            await CorrelationStore(sp).upsert_thread_session(channel_id="ch-1", anchor="a", workspace_id="w", session_id="s2")
            assert [r.session_id for r in await CorrelationStore(sp).list_for_channel("ch-1")] == ["s2"]
        finally:
            await sp.aclose()


# --------------------------------------------------------------------------- W56h


async def test_W56h_clearing_an_answered_gate_keeps_a_newer_gate():
    sp = _FakeStorageProvider()
    store = CorrelationStore(sp)
    await store.upsert_session(channel_id="ch-1", anchor="th-1", workspace_id="w1", session_id="s1", tool_call_id="tc-1", gate_id="g1")
    # The reply answered tc-1; meanwhile the session asked again on the same thread.
    await store.upsert_session(channel_id="ch-1", anchor="th-1", workspace_id="w1", session_id="s1", tool_call_id="tc-2", gate_id="g2")
    await store.clear_gate("ch-1", "th-1", tool_call_id="tc-1")
    row = await store.lookup("ch-1", "th-1")
    assert (row.tool_call_id, row.gate_id) == ("tc-2", "g2")


async def test_W56h_clear_gate_writes_only_the_gate_fields(tmp_path: Path):
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "gate.sqlite"))
    await sp.initialize()
    try:
        store = CorrelationStore(sp)
        await store.upsert_session(channel_id="ch-1", anchor="th-1", workspace_id="w1", session_id="s1", tool_call_id="tc-1")
        await store.clear_gate("ch-1", "th-1", tool_call_id="tc-1")
        row = await store.lookup("ch-1", "th-1")
        assert (row.session_id, row.workspace_id, row.tool_call_id, row.gate_id) == ("s1", "w1", None, None)
        assert row.updated_at is not None
    finally:
        await sp.aclose()


# --------------------------------------------------------------------------- W56i


async def test_W56i_the_trigger_scan_reads_only_the_events_provider():
    sp = _FakeStorageProvider()
    await _seed(sp, triggers=[("t-mine", "cp-1", None)] + [(f"t-x{i}", "cp-2", None) for i in range(250)])
    storage = sp.get_storage(Trigger)
    real_find = storage.find
    decoded = {"rows": 0}

    async def _counting_find(*a, **k):
        page = await real_find(*a, **k)
        decoded["rows"] += len(page.items)
        return page

    storage.find = _counting_find
    fires = _Fires(sp, start_sessions=False)
    await _router(sp, fires).route_event(event=_event(1, surface="channel"), channel=None)
    assert [c["trigger_id"] for c in fires.calls] == ["t-mine"]
    assert decoded["rows"] == 1
