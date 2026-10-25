"""Where a channel session's posts go, and which room messages start sessions (ticket 01a16e37-b98e, items 4 to 7).

The tests began as the measurement probes of the ticket (ruling 1) and were turned into red-first tests where the item changes.
A fake Slack message event drives the real Slack factory handler, the real (never connected) adapter, the real inbound router
and the real ``fire_trigger`` on real SQLite. The outbound side runs the real dispatcher, registry, relay and adapter
``post_prompt`` against a fake Slack web client that records each post with the room and the thread it went to.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from primer.channel.correlation import CorrelationStore
from primer.channel.inbox import ChannelInbox
from primer.channel.reply_binding import SESSION_REPLY_BINDING_KEY
from primer.model.agent import Agent, AgentModel
from primer.model.channel import (
    Channel,
    ChannelProvider,
    ChannelProviderType,
    DiscordChannelProviderConfig,
    SlackChannelConfig,
    SlackChannelProviderConfig,
)
from primer.model.principal import PrincipalRef
from primer.model.provider import SqliteConfig
from primer.model.storage import OffsetPage
from primer.model.trigger import AgentFreshSubConfig, ChannelTriggerConfig, SessionAppendSubConfig, Subscription, Trigger
from primer.model.workspace import Workspace, WorkspaceChannelLink, WorkspaceRuntimeMeta
from primer.model.workspace_session import AgentSessionBinding, PendingSessionMessage, SessionStatus, WorkspaceSession
from primer.storage.q import Q
from primer.storage.sqlite import SqliteStorageProvider
from tests.trigger.conftest import _FakeClaimEngine, _FakeWorkspaceRegistry

ADMIN = PrincipalRef(type="user", id="u-root", display="root", role="admin", source="local")
THREAD = "1700000000.000100"          # the platform thread the inbound message was posted in
OTHER_ANCHOR = "1700000000.999999"    # a thread an explicit target or a workspace binding names


class _BoltApp:
    """Records the handlers ``_install_handlers`` registers, as slack_bolt's decorators would."""

    def __init__(self) -> None:
        self.events: dict = {}

    def _keep(self, into: dict, name: str):
        def deco(fn):
            into[name] = fn
            return fn
        return deco

    def event(self, name):
        return self._keep(self.events, name)

    def action(self, name):
        return self._keep({}, name)

    def view(self, name):
        return self._keep({}, name)


class _SlackWeb:
    """A fake Slack web client: every chat_postMessage is recorded with the room it went to."""

    def __init__(self) -> None:
        self.posts: list[dict] = []

    async def chat_postMessage(self, **kw):
        self.posts.append(kw)
        return {"ts": f"1800000000.{len(self.posts):06d}", "channel": kw.get("channel")}


@pytest.fixture
async def sp(tmp_path):
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "routing.sqlite"))
    await provider.initialize()
    yield provider
    await provider.aclose()


def _fresh(sub_id: str = "sb-1", **fields) -> Subscription:
    """A binding that starts a fresh ``ag-1`` session in ``ws-1`` with the message text."""
    return Subscription(
        id=sub_id, trigger_id="tr-1", config=AgentFreshSubConfig(workspace_id="ws-1", agent_id="ag-1"),
        payload_template="{{ event.text }}", parallelism="queue", created_at=datetime.now(UTC), **fields,
    )


class _World:
    """Two Slack rooms (ch-sl is C01, ch-other is C02) on one provider, a workspace, an agent and a channel trigger on ch-sl."""

    def __init__(self, monkeypatch, sp) -> None:
        pytest.importorskip("slack_bolt")
        self.sp = sp
        self.monkeypatch = monkeypatch
        self.provider = ChannelProvider(
            id="cp-sl", provider=ChannelProviderType.SLACK,
            config=SlackChannelProviderConfig(app_token=SecretStr("xapp-1-test"), bot_token=SecretStr("xoxb-test")),
        )
        self.web = {"ch-sl": _SlackWeb(), "ch-other": _SlackWeb()}
        self.adapters: dict = {}

    async def seed(self, *, chats_enabled: bool = True, interactive: bool = True, subscriptions=None,
                   ws_binding: WorkspaceChannelLink | None = None) -> None:
        from primer.channel.slack import factory
        from primer.channel.slack.adapter import SlackChannelAdapter

        sp = self.sp
        await sp.get_storage(ChannelProvider).create(self.provider)
        for cid, room in (("ch-sl", "C01"), ("ch-other", "C02")):
            channel = Channel(
                id=cid, provider_id="cp-sl", provider=ChannelProviderType.SLACK, external_id=room,
                config=SlackChannelConfig.model_validate({"chats": {"enabled": chats_enabled}}),
            )
            await sp.get_storage(Channel).create(channel)
            adapter = SlackChannelAdapter(
                provider=self.provider, channel=channel, inbox=ChannelInbox(event_bus=None, storage_provider=sp),
                storage_provider=sp, event_bus=None, claim_engine=_FakeClaimEngine(), workspace_registry=_FakeWorkspaceRegistry(),
            )
            adapter._conn = SimpleNamespace(app=SimpleNamespace(client=self.web[cid]))
            self.adapters[cid] = adapter
        entry = SimpleNamespace(adapters_by_channel_id={"C01": self.adapters["ch-sl"], "C02": self.adapters["ch-other"]})
        self.monkeypatch.setattr(factory, "SLACK_CONNECTIONS", SimpleNamespace(entry=lambda _pid: entry))
        self.monkeypatch.setattr(factory, "_HANDLERS_INSTALLED", set())
        self.app = _BoltApp()
        factory._install_handlers("cp-sl", self.app)

        await sp.get_storage(Workspace).create(Workspace(
            id="ws-1", description="w", template_id="t-1", provider_id="p-1", created_at=datetime.now(UTC),
            runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959/", token=SecretStr("t")), reply_binding=ws_binding,
        ))
        await sp.get_storage(Agent).create(Agent(id="ag-1", description="a", model=AgentModel(profile_id="p--m")))
        await sp.get_storage(Trigger).create(Trigger(
            id="tr-1", slug="ch-trigger", name="Channel", created_at=datetime.now(UTC),
            config=ChannelTriggerConfig(provider_id="cp-sl", channel_id="ch-sl", interactive=interactive),
        ))
        for sub in (subscriptions if subscriptions is not None else [_fresh()]):
            await sp.get_storage(Subscription).create(sub)

    async def message(self, text: str, *, thread_ts: str | None = THREAD, ts: str = "1700000001.000001", room: str = "C01") -> None:
        """A person posts ``text`` in ``room`` (C01): in the thread ``thread_ts``, or as a room post when it is None."""
        event = {"channel": room, "ts": ts, "user": "U7", "text": text}
        if thread_ts is not None:
            event["thread_ts"] = thread_ts
        # The bolt context carries the bot user id (the authorization middleware's cached auth.test); the harness passes one so the
        # handler never falls back to the adapter's own auth.test call.
        await self.app.events["message"](event, SimpleNamespace(chat_postEphemeral=AsyncMock()), SimpleNamespace(bot_user_id="U-bot"))

    async def sessions(self) -> list[WorkspaceSession]:
        return list((await self.sp.get_storage(WorkspaceSession).list(OffsetPage(offset=0, length=50))).items)

    def dispatcher(self):
        from primer.api.registries.channel_registry import ChannelRegistry
        from primer.channel.dispatcher import ChannelDispatcher

        reg = ChannelRegistry(
            channel_storage=self.sp.get_storage(Channel), channel_provider_storage=self.sp.get_storage(ChannelProvider),
            inbox=None, storage_provider=self.sp,
        )
        reg._adapters.update(self.adapters)
        return ChannelDispatcher(registry=reg)

    def posts(self, cid: str) -> list[tuple[str | None, str]]:
        """(thread_ts, text) of every post into the room of ``cid``, in order."""
        return [(p.get("thread_ts"), p.get("text", "")) for p in self.web[cid].posts]


async def _say_everything(world, session) -> None:
    """Every kind of post a session makes: a question, an approval request, an ``inform`` and the final result."""
    from primer.agent.inform import SessionInformSink
    from primer.channel.session_relay import post_session_final_result
    from primer.worker.yield_runtime import _dispatch_to_channels

    for tool_name, metadata in (
        ("ask_user", {"prompt": "Which currency?", "tool_call_id": "tc-1"}),
        ("_approval", {"original_call": {"id": "tc-2", "name": "deploy", "arguments": {}}}),
    ):
        yielded = SimpleNamespace(tool_name=tool_name, event_key=f"x:{session.id}:tc", resume_metadata=metadata)
        await _dispatch_to_channels(dispatcher=world.dispatcher(), session=session, yielded=yielded)
    sink = SessionInformSink(dispatcher=world.dispatcher(), workspace_id=session.workspace_id, session_id=session.id, session=session)
    await sink("half way there")
    await post_session_final_result(dispatcher=world.dispatcher(), session=session, storage_provider=world.sp, text="DONE")


def _a_session(sid: str = "s-ws", **metadata) -> WorkspaceSession:
    return WorkspaceSession(
        id=sid, workspace_id="ws-1", binding=AgentSessionBinding(agent_id="ag-1"), status=SessionStatus.RUNNING,
        created_at=datetime.now(UTC), initiated_by=ADMIN, metadata=metadata,
    )


# ---------------------------------------------------------------- items 4 and 5: the anchor goes on every post


async def test_a_session_started_from_a_thread_keeps_its_whole_conversation_in_that_thread(monkeypatch, sp) -> None:
    """The measurement found that the question, the approval request and the inform opened a NEW thread (a ``:thread:`` root
    post) and only the final result came back to the thread the person wrote in."""
    world = _World(monkeypatch, sp)
    await world.seed()
    await world.message("deploy please")
    [session] = await world.sessions()

    await _say_everything(world, session)

    posts = world.posts("ch-sl")
    assert [thread for thread, _text in posts] == [THREAD] * 4, posts
    assert not any(text.startswith(":thread:") for _thread, text in posts), "a new thread was opened"
    assert world.posts("ch-other") == []


async def test_a_workspace_reply_binding_with_an_anchor_posts_into_that_thread(monkeypatch, sp) -> None:
    """Item 5: ``system__set_reply_binding``'s anchor was dropped when the binding was resolved, so every session opened its own thread.
    Round 2, finding 1: the anchor is a SHARED one, so an ask_user question opens the session's own thread (with its own row) and only the
    other posts go into the anchor."""
    world = _World(monkeypatch, sp)
    await world.seed(ws_binding=WorkspaceChannelLink(channel_id="ch-other", anchor=OTHER_ANCHOR))
    session = _a_session()          # not started by a channel message: the workspace's binding answers for it
    await sp.get_storage(WorkspaceSession).create(session)

    await _say_everything(world, session)

    posts = world.posts("ch-other")
    assert [thread for thread, _text in posts if thread == OTHER_ANCHOR] == [OTHER_ANCHOR] * 3, posts
    roots = [text for _thread, text in posts if text.startswith(":thread:")]
    assert roots == [f":thread: Agent session {session.id}"], posts
    # The question is answerable: its own thread is mapped to the session, and the shared anchor is mapped to nothing.
    question_thread = next(t for t, _x in posts if t is not None and t != OTHER_ANCHOR)
    record = await CorrelationStore(sp).lookup("ch-other", question_thread)
    assert record is not None and record.session_id == session.id
    assert await CorrelationStore(sp).lookup("ch-other", OTHER_ANCHOR) is None
    assert world.posts("ch-sl") == []


async def test_a_workspace_reply_binding_without_an_anchor_still_opens_one_thread_per_session(monkeypatch, sp) -> None:
    world = _World(monkeypatch, sp)
    await world.seed(ws_binding=WorkspaceChannelLink(channel_id="ch-other"))
    session = _a_session()
    await sp.get_storage(WorkspaceSession).create(session)

    await _say_everything(world, session)

    posts = world.posts("ch-other")
    root_thread, root_text = posts[0]
    assert root_thread is None and root_text == f":thread: Agent session {session.id}"
    session_thread = "1800000000.000001"
    assert [thread for thread, _text in posts[1:]] == [session_thread] * 4, posts


async def test_discord_posts_every_message_of_a_thread_mapped_session_into_its_thread() -> None:
    """The same path on Discord, with a fake client: the binding's anchor is the thread id, and no thread of the session's own is opened.
    An anchor that is not a Discord id (a Slack thread ts) no longer fails the post: the session's own thread is used."""
    pytest.importorskip("discord")
    from primer.api.registries.channel_registry import ChannelRegistry
    from primer.channel.dispatcher import ChannelDispatcher
    from primer.channel.discord.adapter import DiscordChannelAdapter

    class _Room:
        def __init__(self, room_id: int) -> None:
            self.id = room_id
            self.sent: list[str] = []
            self.threads: list[_Room] = []

        async def send(self, content=None, **_kw):
            self.sent.append(content)
            return SimpleNamespace(id=len(self.sent), create_thread=self._open_thread)

        async def _open_thread(self, *, name, **_kw):
            thread = _Room(9000 + len(self.threads))
            self.threads.append(thread)
            return thread

    parent, thread = _Room(500), _Room(777)
    rooms = {500: parent, 777: thread}
    client = SimpleNamespace(get_channel=lambda room_id: rooms.get(room_id), fetch_channel=AsyncMock(return_value=None))
    provider = ChannelProvider(id="cp-dc", provider=ChannelProviderType.DISCORD, config=DiscordChannelProviderConfig(bot_token=SecretStr("x" * 60)))
    channel = Channel(id="ch-dc", provider_id="cp-dc", provider=ChannelProviderType.DISCORD, external_id="500")
    adapter = DiscordChannelAdapter(provider=provider, channel=channel, inbox=None)
    adapter._client = client

    class _Registry:
        """The real binding resolution over a session's own binding, handing out the one adapter."""

        def __init__(self) -> None:
            self._storage_provider = SimpleNamespace(get_storage=lambda _model: SimpleNamespace(get=AsyncMock(return_value=None)))

        async def get_adapter(self, _channel_id):
            return adapter

        async def route_for_session(self, session):
            return await ChannelRegistry.route_for_session(self, session)

        async def for_session(self, session):
            return await ChannelRegistry.for_session(self, session)

    from primer.model.envelope import PromptEnvelope

    def _post(kind: str, text: str, *, session_id: str, anchor: str):
        session = SimpleNamespace(
            id=session_id, workspace_id="ws-1", metadata={SESSION_REPLY_BINDING_KEY: {"channel_id": "ch-dc", "anchor": anchor}},
        )
        envelope = PromptEnvelope(
            kind=kind, workspace_id="ws-1", session_id=session_id, tool_call_id="tc-1", prompt=text,
            response_schema=None, choices=None, timeout_at_iso=None,
        )
        return ChannelDispatcher(registry=_Registry()).dispatch_prompt(envelope=envelope, session=session)

    for kind, text in (("ask_user", "Which currency?"), ("inform", "half way there")):
        results = await _post(kind, text, session_id="s-dc", anchor="777")
        assert all("error" not in r for r in results), results
    assert thread.sent == ["Which currency?", "half way there"]
    assert parent.sent == [] and parent.threads == []

    results = await _post("inform", "still here", session_id="s-dc2", anchor=THREAD)
    assert all("error" not in r for r in results), results
    assert parent.sent == [":thread: Agent session s-dc2"] and parent.threads[0].sent == ["still here"]


async def test_discord_a_shared_anchor_question_opens_the_sessions_own_thread() -> None:
    """Round 2, finding 1, on Discord: an ask_user on a shared anchor (an explicit reply target) opens the session's own thread;
    every other post goes into the shared anchor."""
    pytest.importorskip("discord")
    from primer.api.registries.channel_registry import ChannelRegistry
    from primer.channel.dispatcher import ChannelDispatcher
    from primer.channel.discord.adapter import DiscordChannelAdapter

    class _Room:
        def __init__(self, room_id: int) -> None:
            self.id = room_id
            self.sent: list[str] = []
            self.threads: list[_Room] = []

        async def send(self, content=None, **_kw):
            self.sent.append(content)
            return SimpleNamespace(id=len(self.sent), create_thread=self._open_thread)

        async def _open_thread(self, *, name, **_kw):
            thread = _Room(9000 + len(self.threads))
            self.threads.append(thread)
            return thread

    parent, shared = _Room(500), _Room(777)
    rooms = {500: parent, 777: shared}
    client = SimpleNamespace(get_channel=lambda room_id: rooms.get(room_id), fetch_channel=AsyncMock(return_value=None))
    provider = ChannelProvider(id="cp-dc", provider=ChannelProviderType.DISCORD, config=DiscordChannelProviderConfig(bot_token=SecretStr("x" * 60)))
    channel = Channel(id="ch-dc", provider_id="cp-dc", provider=ChannelProviderType.DISCORD, external_id="500")
    adapter = DiscordChannelAdapter(provider=provider, channel=channel, inbox=None)
    adapter._client = client

    class _Registry:
        _storage_provider = SimpleNamespace(get_storage=lambda _model: SimpleNamespace(get=AsyncMock(return_value=None)))

        async def get_adapter(self, _channel_id):
            return adapter

        async def route_for_session(self, session):
            return await ChannelRegistry.route_for_session(self, session)

    from primer.model.envelope import PromptEnvelope

    def _session(session_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            id=session_id, workspace_id="ws-1",
            metadata={SESSION_REPLY_BINDING_KEY: {"channel_id": "ch-dc", "anchor": "777", "shared_anchor": True}},
        )

    def _envelope(kind: str, text: str, session_id: str) -> PromptEnvelope:
        return PromptEnvelope(
            kind=kind, workspace_id="ws-1", session_id=session_id, tool_call_id="tc-1", prompt=text,
            response_schema=None, choices=None, timeout_at_iso=None,
        )

    dispatcher = ChannelDispatcher(registry=_Registry())
    # The question opens the session's own thread off the room, not the shared one.
    results = await dispatcher.dispatch_prompt(envelope=_envelope("ask_user", "Which currency?", "s-sh"), session=_session("s-sh"))
    assert all("error" not in r for r in results), results
    assert parent.threads and parent.threads[0].sent == ["Which currency?"]
    assert shared.sent == []
    # The inform goes into the shared anchor.
    results = await dispatcher.dispatch_prompt(envelope=_envelope("inform", "half way there", "s-sh"), session=_session("s-sh"))
    assert all("error" not in r for r in results), results
    assert shared.sent == ["half way there"]


# ---------------------------------------------------------------- round 2, finding 1: a shared anchor is not a conversation


async def test_a_shared_anchor_keeps_each_sessions_questions_in_its_own_thread(monkeypatch, sp) -> None:
    """Two sessions on the workspace binding's anchor: the round 1 dispatcher put the anchor on every post, so both questions went
    into the same thread and the second ask_user's correlation row overwrote the first - a reply to the first question would steer
    the second session. An ask_user on a shared anchor now opens the session's own thread, with its own row; the shared anchor
    carries no thread-to-session row at all, so a plain reply typed there steers nothing (it is an ordinary channel message for
    that channel's bindings)."""
    from primer.worker.yield_runtime import _dispatch_to_channels

    world = _World(monkeypatch, sp)
    await world.seed(ws_binding=WorkspaceChannelLink(channel_id="ch-other", anchor=OTHER_ANCHOR))
    storage = sp.get_storage(WorkspaceSession)
    first, second = _a_session("s-first"), _a_session("s-second")
    await storage.create(first)
    await storage.create(second)

    for session in (first, second):
        yielded = SimpleNamespace(tool_name="ask_user", event_key=f"x:{session.id}:tc", resume_metadata={"prompt": "Which currency?", "tool_call_id": "tc-1"})
        await _dispatch_to_channels(dispatcher=world.dispatcher(), session=session, yielded=yielded)

    posts = world.posts("ch-other")
    assert [thread for thread, _text in posts if thread == OTHER_ANCHOR] == [], posts
    roots = [text for _thread, text in posts if text.startswith(":thread:")]
    assert roots == [":thread: Agent session s-first", ":thread: Agent session s-second"], posts
    # Both questions are answerable from their own threads: the question threads, in order, are mapped to first and second.
    store = CorrelationStore(sp)
    question_threads = [t for t, _x in posts if t is not None]
    assert len(question_threads) == 2 and question_threads[0] != question_threads[1], posts
    for session, thread in ((first, question_threads[0]), (second, question_threads[1])):
        record = await store.lookup("ch-other", thread)
        assert record is not None and record.session_id == session.id, (session.id, record)
    # The shared anchor is mapped to nothing, and a later message there starts or feeds no session.
    assert await store.lookup("ch-other", OTHER_ANCHOR) is None
    before = [s.id for s in await world.sessions()]
    await world.message("a plain reply in the shared thread", thread_ts=OTHER_ANCHOR, room="C02")
    assert [s.id for s in await world.sessions()] == before


# ---------------------------------------------------------------- round 2, finding 2: the binding written after the turn loaded the row


async def test_a_binding_written_after_the_turn_loaded_the_row_still_mutes_the_first_turn(monkeypatch, sp) -> None:
    """The turn loads the session row once; the inbound router writes the session's reply binding only after fire_trigger returns. On the
    first turn of a session the event started, the in-memory row therefore misses the binding, and a silent reply_target leaked
    through the workspace's standing binding. resolve_reply_binding re-reads the leaf from storage, so nothing posts."""
    world = _World(monkeypatch, sp)
    await world.seed(ws_binding=WorkspaceChannelLink(channel_id="ch-other", anchor=OTHER_ANCHOR))
    session = _a_session("s-stale")
    storage = sp.get_storage(WorkspaceSession)
    await storage.create(session)
    # The turn's copy of the row (``session`` above) was loaded before this write: it carries no binding.
    await storage.patch_if(
        session.id, None,
        set_paths={("metadata", SESSION_REPLY_BINDING_KEY): {"channel_id": "ch-sl", "anchor": THREAD, "quiet": True, "silent": True}},
        where={"workspace_id": ["ws-1"]},
    )

    await _say_everything(world, session)

    assert world.posts("ch-other") == [], "the silent binding leaked through the workspace's binding"
    assert world.posts("ch-sl") == []


# ---------------------------------------------------------------- item 4: the binding's reply_target


async def _started_by(world, text: str, *, thread_ts: str | None = THREAD) -> WorkspaceSession:
    await world.message(text, thread_ts=thread_ts)
    [session] = await world.sessions()
    return session


async def test_an_explicit_reply_target_takes_every_post_to_its_channel_and_thread(monkeypatch, sp) -> None:
    """The measurement found the target stored and never read: everything went to the source thread.
    Round 2, finding 1: the target's anchor is a SHARED one, so the question opens the session's own thread in the target's channel."""
    world = _World(monkeypatch, sp)
    await world.seed(subscriptions=[_fresh(reply_target={"channel_id": "ch-other", "anchor": OTHER_ANCHOR})])
    session = await _started_by(world, "deploy please")

    await _say_everything(world, session)

    assert [thread for thread, _text in world.posts("ch-other") if thread == OTHER_ANCHOR] == [OTHER_ANCHOR] * 3, world.posts("ch-other")
    assert world.posts("ch-sl") == []
    # The question opened the session's own thread in the target's channel, mapped to the session; the shared anchor maps nothing.
    question_thread = next(t for t, _x in world.posts("ch-other") if t is not None and t != OTHER_ANCHOR)
    record = await CorrelationStore(sp).lookup("ch-other", question_thread)
    assert record is not None and record.session_id == session.id
    assert await CorrelationStore(sp).lookup("ch-other", OTHER_ANCHOR) is None
    # The conversation is still the source thread: the next message there goes to the same session.
    mapping = await CorrelationStore(sp).lookup("ch-sl", THREAD)
    assert mapping is not None and mapping.session_id == session.id


async def test_source_thread_answers_a_room_post_in_its_room(monkeypatch, sp) -> None:
    """A room post has no thread, so source_thread falls back to the room: on Slack, a new thread per session there."""
    world = _World(monkeypatch, sp)
    await world.seed(subscriptions=[_fresh(reply_target="source_thread")])
    session = await _started_by(world, "deploy please", thread_ts=None)

    await _say_everything(world, session)

    posts = world.posts("ch-sl")
    assert posts[0] == (None, f":thread: Agent session {session.id}")
    assert [thread for thread, _text in posts[1:]] == ["1800000000.000001"] * 4, posts


async def test_with_no_reply_target_a_room_post_still_answers_through_the_workspace_binding(monkeypatch, sp) -> None:
    """The default is unchanged: a room post maps nothing, and its session uses the workspace's reply binding. The binding's anchor is a
    SHARED one (round 2, finding 1), so the question opens the session's own thread and the other posts go into the anchor."""
    world = _World(monkeypatch, sp)
    await world.seed(ws_binding=WorkspaceChannelLink(channel_id="ch-other", anchor=OTHER_ANCHOR))
    session = await _started_by(world, "deploy please", thread_ts=None)
    assert SESSION_REPLY_BINDING_KEY not in session.metadata

    await _say_everything(world, session)

    posts = world.posts("ch-other")
    assert [thread for thread, _text in posts if thread == OTHER_ANCHOR] == [OTHER_ANCHOR] * 3, posts
    question_thread = next(t for t, _x in posts if t is not None and t != OTHER_ANCHOR)
    record = await CorrelationStore(sp).lookup("ch-other", question_thread)
    assert record is not None and record.session_id == session.id
    assert await CorrelationStore(sp).lookup("ch-other", OTHER_ANCHOR) is None
    assert world.posts("ch-sl") == []


async def test_source_room_answers_in_a_thread_of_its_own_in_the_room(monkeypatch, sp) -> None:
    world = _World(monkeypatch, sp)
    await world.seed(subscriptions=[_fresh(reply_target="source_room")])
    session = await _started_by(world, "deploy please")

    await _say_everything(world, session)

    posts = world.posts("ch-sl")
    assert posts[0] == (None, f":thread: Agent session {session.id}")
    assert THREAD not in [thread for thread, _text in posts], posts
    assert (await CorrelationStore(sp).lookup("ch-sl", THREAD)).session_id == session.id


async def test_none_posts_nothing_not_even_through_the_workspace_binding(monkeypatch, sp) -> None:
    world = _World(monkeypatch, sp)
    await world.seed(subscriptions=[_fresh(reply_target="none")], ws_binding=WorkspaceChannelLink(channel_id="ch-other"))
    session = await _started_by(world, "deploy please")

    await _say_everything(world, session)

    assert world.posts("ch-sl") == [] and world.posts("ch-other") == []
    assert session.metadata[SESSION_REPLY_BINDING_KEY]["silent"] is True


async def test_a_stored_dm_sender_still_loads_and_answers_in_the_source_thread_with_one_warning(monkeypatch, sp, caplog) -> None:
    """dm_sender is refused on save now; a row saved before still lists and fires, as source_thread, and says so once."""
    from primer.model.common import stored_row_context

    stored = Subscription.model_validate({**_fresh().model_dump(mode="json"), "reply_target": "dm_sender"}, context=stored_row_context())
    world = _World(monkeypatch, sp)
    await world.seed(subscriptions=[stored])
    assert (await sp.get_storage(Subscription).get("sb-1")).reply_target == "dm_sender"
    with pytest.raises(ValueError, match="dm_sender"):
        _fresh(reply_target="dm_sender")

    from primer.channel import event_dispatch

    monkeypatch.setattr(event_dispatch, "_DM_SENDER_REPORTED", set())
    caplog.set_level("WARNING", logger="primer.channel.event_dispatch")
    await world.message("one", thread_ts=THREAD, ts="1700000001.000001")
    await world.message("two", thread_ts="1700000000.000200", ts="1700000001.000002")

    sessions = await world.sessions()
    assert sorted(s.metadata[SESSION_REPLY_BINDING_KEY]["anchor"] for s in sessions) == sorted([THREAD, "1700000000.000200"])
    assert all(s.metadata[SESSION_REPLY_BINDING_KEY]["channel_id"] == "ch-sl" for s in sessions)
    warnings = [r for r in caplog.records if "dm_sender" in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]


# ---------------------------------------------------------------- item 6: chats.enabled gates the bindings that start a session


USER = PrincipalRef(type="user", id="u-1", display="dee", role="user", source="local")


async def _queued(sp, session_id: str) -> list[str]:
    """The texts waiting in a session's queue for its drain, oldest first."""
    page = await sp.get_storage(PendingSessionMessage).find(
        Q(PendingSessionMessage).where("session_id", session_id).build(), OffsetPage(offset=0, length=50),
    )
    rows = sorted(page.items, key=lambda row: row.enqueued_at)
    return ["\n".join(part["text"] for part in row.parts if part.get("type") == "text") for row in rows]


def _skipped_room_disabled() -> float:
    from primer.observability import metrics

    counter = getattr(metrics, "channel_events_skipped_total", None)      # absent before the gate existed: the red run reads 0
    return counter.labels(reason="room_disabled")._value.get() if counter is not None else 0.0


async def test_a_room_with_chats_disabled_starts_no_session_but_a_session_append_binding_still_feeds(monkeypatch, sp) -> None:
    """The measurement found a disabled room starting sessions like any other. Now the bindings that start a session do not fire there,
    and a binding that feeds a running session still does."""
    world = _World(monkeypatch, sp)
    append = Subscription(
        id="sb-append", trigger_id="tr-1", config=SessionAppendSubConfig(session_id="s-run"), payload_template="{{ event.text }}",
        parallelism="queue", created_at=datetime.now(UTC),
    )
    await world.seed(chats_enabled=False, subscriptions=[_fresh(), append])
    running = WorkspaceSession(
        id="s-run", workspace_id="ws-1", binding=AgentSessionBinding(agent_id="ag-1"), status=SessionStatus.RUNNING,
        turn_status="running", created_at=datetime.now(UTC), initiated_by=USER,
    )
    await sp.get_storage(WorkspaceSession).create(running)
    before = _skipped_room_disabled()

    await world.message("a room post", thread_ts=None, ts="1700000001.000001")
    await world.message("in a thread", thread_ts=THREAD, ts="1700000001.000002")

    assert [s.id for s in await world.sessions()] == ["s-run"], "a session was started in a room whose chats.enabled is false"
    assert await _queued(sp, "s-run") == ["a room post", "in a thread"]
    assert _skipped_room_disabled() == before + 2
    fresh = await sp.get_storage(Subscription).get("sb-1")
    assert fresh.last_fired_at is None, "the dropped binding was dispatched"


async def test_a_room_with_chats_disabled_still_feeds_the_session_its_thread_is_mapped_to(monkeypatch, sp) -> None:
    world = _World(monkeypatch, sp)
    await world.seed(chats_enabled=False)
    running = WorkspaceSession(
        id="s-run", workspace_id="ws-1", binding=AgentSessionBinding(agent_id="ag-1"), status=SessionStatus.RUNNING,
        turn_status="running", created_at=datetime.now(UTC), initiated_by=USER,
    )
    await sp.get_storage(WorkspaceSession).create(running)
    await CorrelationStore(sp).upsert_thread_session(channel_id="ch-sl", anchor=THREAD, workspace_id="ws-1", session_id="s-run")

    await world.message("any update?")

    assert [s.id for s in await world.sessions()] == ["s-run"]
    assert await _queued(sp, "s-run") == ["any update?"]


async def test_a_room_with_chats_enabled_starts_sessions_and_counts_nothing(monkeypatch, sp) -> None:
    world = _World(monkeypatch, sp)
    await world.seed(chats_enabled=True)
    before = _skipped_room_disabled()

    await world.message("deploy please")

    assert len(await world.sessions()) == 1
    assert _skipped_room_disabled() == before


async def test_a_room_with_no_listener_is_not_heard_on_the_providers_shared_connection(monkeypatch, sp) -> None:
    """Listening is unchanged and is per room: the connection is the provider's, but a message from a room whose adapter is not
    registered on it (boot starts only rooms with chats.enabled) is dropped before anything is routed."""
    world = _World(monkeypatch, sp)
    await world.seed()
    event = {"channel": "C03", "ts": "1700000001.000001", "user": "U7", "text": "anyone?"}

    await world.app.events["message"](event, SimpleNamespace(chat_postEphemeral=AsyncMock()), SimpleNamespace(bot_user_id="U-bot"))

    assert await world.sessions() == []


# ---------------------------------------------------------------- item 7: relay_mode is gone; the trigger decides


@pytest.mark.parametrize("interactive", [True, False])
async def test_how_much_a_conversation_hears_is_the_triggers_interactive_flag_and_relay_mode_is_gone(monkeypatch, sp, interactive) -> None:
    """The measurement found relay_mode stored and never read: ``interactive`` alone decided, in all four mixes. The knob is removed."""
    from pydantic import ValidationError

    from primer.model.envelope import RELAY_EVERY_TURN_KEY

    with pytest.raises(ValidationError, match="relay_mode"):
        SlackChannelConfig.model_validate({"chats": {"enabled": True, "relay_mode": "all"}})
    world = _World(monkeypatch, sp)
    await world.seed(interactive=interactive)
    session = await _started_by(world, "deploy please")

    assert bool(session.metadata.get(RELAY_EVERY_TURN_KEY)) is interactive
    assert session.metadata[SESSION_REPLY_BINDING_KEY]["quiet"] is (not interactive)
