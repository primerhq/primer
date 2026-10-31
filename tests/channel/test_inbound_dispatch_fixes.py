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


# --------------------------------------------------------------------------- W56b


async def test_W56b_two_attachments_with_one_name_both_land():
    from primer.channel.media_in import land_media_in_workspace

    ws = _Workspace()
    paths = await land_media_in_workspace(
        workspace=ws, fire_id="f1", artifact_storage=None,
        parts=[_Part("image/png", "image.png", b"A"), _Part("image/png", "image.png", b"B"), _Part("image/png", "c.png", b"C")],
    )
    assert paths == ["media/f1/image.png", "media/f1/1-image.png", "media/f1/c.png"]
    assert ws.files == {"media/f1/image.png": b"A", "media/f1/1-image.png": b"B", "media/f1/c.png": b"C"}


# --------------------------------------------------------------------------- W33


async def test_W33_a_redelivered_event_is_routed_once():
    sp = _FakeStorageProvider()
    await _seed(sp)
    fires = _Fires(sp, start_sessions=False)
    first = await _router(sp, fires).route_event(event=_event(1, surface="channel"), channel=None)
    again = await _router(sp, fires).route_event(event=_event(1, surface="channel"), channel=None)
    assert first.kind == "fired" and again.kind == "ignored"
    assert len(fires.calls) == 1


async def test_W33_a_redelivery_while_the_first_is_still_routing_is_dropped():
    sp = _FakeStorageProvider()
    await _seed(sp)
    fires = _Fires(sp, delay=0.05, start_sessions=False)
    router = _router(sp, fires)
    await asyncio.gather(*(router.route_event(event=_event(1, surface="channel"), channel=None) for _ in range(3)))
    assert len(fires.calls) == 1


async def test_W33_an_event_whose_routing_raised_can_be_redelivered(monkeypatch):
    sp = _FakeStorageProvider()
    await _seed(sp)
    fires = _Fires(sp, start_sessions=False)
    router = _router(sp, fires)
    real = router._resolve_channel_triggers
    calls = {"n": 0}

    async def _flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("storage blip")
        return await real(*a, **k)

    monkeypatch.setattr(router, "_resolve_channel_triggers", _flaky)
    with pytest.raises(RuntimeError):
        await router.route_event(event=_event(1, surface="channel"), channel=None)
    assert (await router.route_event(event=_event(1, surface="channel"), channel=None)).kind == "fired"


def test_W33_the_seen_set_is_bounded(monkeypatch):
    monkeypatch.setattr(ed, "_SEEN_EVENTS_MAX", 50)
    for i in range(500):
        ed._first_delivery(("cp-1", "777", "message.posted", f"m{i}"))
    assert len(ed._SEEN_EVENTS) <= 50


# --------------------------------------------------------------------------- W6


async def test_W6_two_quick_messages_in_a_new_conversation_start_one_session(steers):
    sp = _FakeStorageProvider()
    await _seed(sp)
    fires = _Fires(sp, delay=0.05)
    router = _router(sp, fires)
    first, second = await asyncio.gather(
        router.route_event(event=_event(1, text="hi"), channel=None),
        router.route_event(event=_event(2, text="can you check the build?"), channel=None),
    )
    assert fires.sessions == ["s1"]
    assert (first.kind, first.session_id) == ("fired", "s1")
    assert (second.kind, second.session_id) == ("steer", "s1")
    assert steers == [("s1", "can you check the build?")]


async def test_W6_quick_replies_in_a_mapped_thread_steer_in_arrival_order(monkeypatch):
    sp = _FakeStorageProvider()
    await _seed(sp)
    await CorrelationStore(sp).upsert_thread_session(channel_id="ch-1", anchor="th-1", workspace_id="w1", session_id="s9")
    order: list[str] = []

    async def _slow_first(**kw):
        if kw["text"] == "one":
            await asyncio.sleep(0.05)
        order.append(kw["text"])
        return SteerDelivery(outcome="queued", session_id=kw["session_id"])

    monkeypatch.setattr(ed, "deliver_steer", _slow_first)
    router = _router(sp, _Fires(sp))
    await asyncio.gather(*(
        router.route_event(event=_event(i, text=t, surface="thread", anchor="th-1"), channel=None)
        for i, t in ((1, "one"), (2, "two"))
    ))
    assert order == ["one", "two"]


async def test_W6_a_conversation_claimed_by_another_process_steers_into_its_session(steers, monkeypatch):
    """Another process claimed the conversation and is starting its session: this one waits and steers into it."""
    monkeypatch.setattr(ed, "_CLAIM_POLL_SECONDS", 0.01)
    sp = _FakeStorageProvider()
    await _seed(sp)
    other = CorrelationStore(sp)
    assert await other.claim_thread(channel_id="ch-1", anchor="dm:u1") is True
    fires = _Fires(sp)
    routed = asyncio.create_task(_router(sp, fires).route_event(event=_event(2, text="second"), channel=None))
    await asyncio.sleep(0.05)
    assert not routed.done()  # waiting on the other process's session
    await other.upsert_thread_session(channel_id="ch-1", anchor="dm:u1", workspace_id="w1", session_id="s-other")
    out = await asyncio.wait_for(routed, 5)
    assert (out.kind, out.session_id) == ("steer", "s-other")
    assert fires.calls == [] and steers == [("s-other", "second")]
    assert len(await CorrelationStore(sp).list_for_channel("ch-1")) == 1


async def test_W6_a_claim_that_never_resolves_falls_back_to_a_fresh_fire(steers, monkeypatch):
    """A claimer that died mid-fire must not silence the conversation forever."""
    monkeypatch.setattr(ed, "_CLAIM_POLL_SECONDS", 0.01)
    monkeypatch.setattr(ed, "_CLAIM_WAIT_SECONDS", 0.1)
    sp = _FakeStorageProvider()
    await _seed(sp)
    assert await CorrelationStore(sp).claim_thread(channel_id="ch-1", anchor="dm:u1") is True
    out = await _router(sp, _Fires(sp)).route_event(event=_event(2), channel=None)
    assert (out.kind, out.session_id) == ("fired", "s1")
    row = await CorrelationStore(sp).lookup("ch-1", "dm:u1")
    assert row.session_id == "s1"
    assert len(await CorrelationStore(sp).list_for_channel("ch-1")) == 1


async def test_W6_the_claim_is_released_when_the_fire_maps_nothing():
    sp = _FakeStorageProvider()
    await _seed(sp)
    await _router(sp, _Fires(sp, start_sessions=False)).route_event(event=_event(1), channel=None)
    assert await CorrelationStore(sp).lookup("ch-1", "dm:u1") is None


async def test_W6_the_claim_is_released_when_every_fire_raises():
    sp = _FakeStorageProvider()
    await _seed(sp)

    async def _boom(**_kw):
        raise RuntimeError("fire failed")

    out = await _router(sp, _boom).route_event(event=_event(1), channel=None)
    assert out.kind == "ignored"
    assert await CorrelationStore(sp).lookup("ch-1", "dm:u1") is None


async def test_W6_claiming_is_a_conditional_insert(tmp_path: Path):
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "claim.sqlite"))
    await sp.initialize()
    try:
        a, b = CorrelationStore(sp), CorrelationStore(sp)
        won = await asyncio.gather(*(s.claim_thread(channel_id="ch-1", anchor="th-1") for s in (a, b, a, b)))
        assert sorted(won) == [False, False, False, True]
        await a.upsert_thread_session(channel_id="ch-1", anchor="th-1", workspace_id="w1", session_id="s1")
        rows = await a.list_for_channel("ch-1")
        assert [(r.session_id, r.workspace_id) for r in rows] == [("s1", "w1")]
        # A conversation that is already mapped cannot be claimed.
        assert await b.claim_thread(channel_id="ch-1", anchor="th-1") is False
    finally:
        await sp.aclose()


async def test_W6_a_claim_conflicts_with_a_mapping_written_by_another_path(tmp_path: Path):
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "claim2.sqlite"))
    await sp.initialize()
    try:
        store = CorrelationStore(sp)
        await store.upsert_thread_session(channel_id="ch-1", anchor="th-1", workspace_id="w1", session_id="s1")
        assert await store.claim_thread(channel_id="ch-1", anchor="th-1") is False
        assert [r.session_id for r in await store.list_for_channel("ch-1")] == ["s1"]
    finally:
        await sp.aclose()


def test_claim_rows_are_correlation_rows_with_no_session():
    """A claim is the conversation's own row before its session exists: kind session, no session, no gate."""
    row = ChannelCorrelation(channel_id="ch-1", anchor="a")
    assert (row.kind, row.session_id, row.tool_call_id) == ("session", None, None)


# --------------------------------------------------------------------------- W9


async def test_W9_a_fresh_conversation_lands_its_attachments_before_the_fire():
    sp = _FakeStorageProvider()
    await _seed(sp, workspace_id="w-target")
    registry = _Registry()
    fires = _Fires(sp)
    await _router(sp, fires, registry=registry).route_event(
        event=_event(7, text="what is wrong here?"), channel=None, media_parts=[_Part("image/png", "shot.png", b"PNG")],
    )
    assert registry.workspaces["w-target"].files == {"media/m7/shot.png": b"PNG"}
    assert fires.calls[0]["extra_context"]["event"]["text"] == (
        "what is wrong here?\n\nAttachments in this workspace:\n- media/m7/shot.png"
    )


async def test_W9_attachments_land_in_every_workspace_a_fire_starts_a_session_in():
    sp = _FakeStorageProvider()
    await _seed(sp, triggers=[("t1", "cp-1", "ch-1"), ("t2", "cp-1", None)], workspace_id="w-a")
    await sp.get_storage(Subscription).create(Subscription(
        id="sub-2", trigger_id="t2", created_at=datetime.now(UTC),
        config=AgentFreshSubConfig(workspace_id="w-b", agent_id="ag1"),
    ))
    registry = _Registry()
    await _router(sp, _Fires(sp, start_sessions=False), registry=registry).route_event(
        event=_event(8, surface="channel"), channel=None, media_parts=[_Part("image/png", "x.png", b"X")],
    )
    assert {w: ws.files for w, ws in registry.workspaces.items()} == {
        "w-a": {"media/m8/x.png": b"X"}, "w-b": {"media/m8/x.png": b"X"},
    }


# --------------------------------------------------------------------------- W56g


async def test_W56g_a_voice_message_is_transcribed_through_the_apps_stt_registry(steers, monkeypatch):
    import primer.api.registries.speech_registry as speech_registry
    from primer.model.providers.speech import SpeechToTextProvider
    from primer.model.speech import (
        ACTIVE_SPEECH_CONFIG_ID,
        ActiveSpeechConfig,
        Transcription,
    )

    class _NoThrowaway:
        def __init__(self, *a, **k):
            raise AssertionError("a throwaway STT registry was built")

    monkeypatch.setattr(speech_registry, "SpeechRegistry", _NoThrowaway)

    class _Adapter:
        async def transcribe(self, *, model, audio, filename, mimetype):
            return Transcription(text=f"heard {filename} with {model}")

    class _AppRegistry:
        def __init__(self):
            self.gets: list[str] = []

        async def get(self, provider_id):
            self.gets.append(provider_id)
            return _Adapter()

    sp = _FakeStorageProvider()
    await _seed(sp)
    await sp.get_storage(ActiveSpeechConfig).create(ActiveSpeechConfig(id=ACTIVE_SPEECH_CONFIG_ID, stt_provider_id="stt-1"))
    sp.get_storage(SpeechToTextProvider)._data["stt-1"] = SpeechToTextProvider.model_construct(id="stt-1", default_model="whisper-1")
    await CorrelationStore(sp).upsert_thread_session(channel_id="ch-1", anchor="dm:u1", workspace_id="w1", session_id="s9")
    app_registry = _AppRegistry()
    await _router(sp, _Fires(sp), registry=_Registry(), stt_registry=app_registry).route_event(
        event=_event(3, text=None), channel=None, media_parts=[_Part("audio/ogg", "note.ogg", b"OggS")],
    )
    assert app_registry.gets == ["stt-1"]
    assert steers[0][1].startswith("heard note.ogg with whisper-1")


async def test_W56g_the_inbound_router_hands_the_registry_to_the_event_router(monkeypatch):
    from primer.channel.inbound_router import ChannelInboundRouter

    seen: list = []
    real_init = ChannelEventRouter.__init__

    def _init(self, **kw):
        seen.append(kw.get("stt_registry"))
        real_init(self, **kw)

    async def _route(self, **_kw):
        return ed.ChannelRouteOutcome(kind="ignored")

    monkeypatch.setattr(ChannelEventRouter, "__init__", _init)
    monkeypatch.setattr(ChannelEventRouter, "route_event", _route)
    marker = object()
    sp = _FakeStorageProvider()
    await ChannelInboundRouter(sp, CorrelationStore(sp), stt_registry=marker).route_event(event=_event(1), channel=None)
    assert seen == [marker]


async def test_W56g_the_channel_registry_gives_its_adapters_the_stt_registry(monkeypatch):
    from pydantic import SecretStr

    from primer.api.registries import channel_registry as reg_mod
    from primer.api.registries.channel_registry import ChannelRegistry
    from primer.channel.adapter import ChannelAdapter
    from primer.model.channel import ChannelProvider, TelegramChannelProviderConfig

    class _Adapter:
        stt_registry = None

        async def initialize(self):
            pass

    async def _build(*_a, **_k):
        return _Adapter()

    monkeypatch.setattr(reg_mod, "build_adapter", _build)
    sp = _FakeStorageProvider()
    await sp.get_storage(ChannelProvider).create(ChannelProvider(
        id="cp-1", provider=ChannelProviderType.TELEGRAM,
        config=TelegramChannelProviderConfig(bot_token=SecretStr("123456:abcdefghijklmnopqrstuvwxyz123456")),
    ))
    await _seed(sp, triggers=())
    registry = ChannelRegistry(
        channel_storage=sp.get_storage(Channel), channel_provider_storage=sp.get_storage(ChannelProvider),
        inbox=None, storage_provider=sp,
    )
    marker = object()
    registry.set_session_wiring(workspace_registry=None, scheduler=None, stt_registry=marker)
    adapter = await registry.get_adapter("ch-1")
    assert adapter.stt_registry is marker
    assert ChannelAdapter.stt_registry is None  # the declared default every adapter starts from
