"""A reply in a Slack or Discord thread steers the session the thread is mapped to (ticket 01a16e37-a555).

The two factories' ``_on_message`` took ANY reply in a mapped thread for the answer to a question, even when none was open: the text was
published to ``ask_user:<sid>:None``, which nothing waits on, and the thread's mapping was deleted (``clear``), so the next message there started
a new session. A real answer deleted the mapping too. Now a reply with no open question goes to the router's steer path (the one a Telegram DM
already takes), and an answer clears only the question (``clear_gate``): the thread stays mapped to its session either way.

Fake platform events drive the real handlers, the real (never connected) adapters, the real inbox, router and steer delivery, on real SQLite.
The session was started by an admin and the platform user is nobody primer knows: a channel reply is not ranked (by design, #880), and a gate
routed to specific approvers still refuses that user.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from primer.bus.in_memory import InMemoryEventBus
from primer.channel.adapter import APPROVAL_ROUTED_NOTICE, QUESTION_STALE_NOTICE
from primer.channel.correlation import CorrelationStore
from primer.channel.inbox import ChannelInbox
from primer.model.channel import (
    Channel,
    ChannelProvider,
    ChannelProviderType,
    DiscordChannelProviderConfig,
    SlackChannelProviderConfig,
)
from primer.model.principal import PrincipalRef
from primer.model.provider import SqliteConfig
from primer.model.storage import OffsetPage
from primer.model.workspace_session import AgentSessionBinding, PendingSessionMessage, SessionStatus, WorkspaceSession
from primer.storage.q import Q
from primer.storage.sqlite import SqliteStorageProvider

WS, SID, TCID = "ws-1", "s-1", "tc-1"
GATE, NEWER_GATE = "c" * 32, "d" * 32
ADMIN = PrincipalRef(type="user", id="u-root", display="root", role="admin", source="local")


class _RecordingBus(InMemoryEventBus):
    """The in-memory bus, keeping every publish for the test to read."""

    def __init__(self) -> None:
        super().__init__()
        self.published: list[tuple[str, dict]] = []

    async def publish(self, event_key: str, payload: dict | None = None) -> None:
        self.published.append((event_key, dict(payload or {})))
        await super().publish(event_key, payload)

    def keys(self, prefix: str) -> list[str]:
        return [key for key, _ in self.published if key.startswith(prefix)]


class _BoltApp:
    """Records the handlers ``_install_handlers`` registers, as slack_bolt's decorators would."""

    def __init__(self) -> None:
        self.events: dict = {}
        self.actions: dict = {}

    def _keep(self, into: dict, name: str):
        def deco(fn):
            into[name] = fn
            return fn
        return deco

    def event(self, name):
        return self._keep(self.events, name)

    def action(self, name):
        return self._keep(self.actions, name)

    def view(self, name):
        return self._keep({}, name)


class _Slack:
    """The Slack handlers on a fake bolt app, an unconnected SlackChannelAdapter and a fake web client."""

    channel_id = "ch-sl"
    anchor = "1700000000.000100"                 # the thread's root ts

    def __init__(self, monkeypatch, sp, bus) -> None:
        pytest.importorskip("slack_bolt")
        from primer.channel.slack import factory
        from primer.channel.slack.adapter import SlackChannelAdapter

        provider = ChannelProvider(
            id="cp-sl", provider=ChannelProviderType.SLACK,
            config=SlackChannelProviderConfig(app_token=SecretStr("xapp-1-test"), bot_token=SecretStr("xoxb-test")),
        )
        channel = Channel(id=self.channel_id, provider_id="cp-sl", provider=ChannelProviderType.SLACK, external_id="C01")
        adapter = SlackChannelAdapter(
            provider=provider, channel=channel, inbox=ChannelInbox(event_bus=bus, storage_provider=sp), storage_provider=sp, event_bus=bus,
        )
        entry = SimpleNamespace(adapters_by_channel_id={"C01": adapter})
        monkeypatch.setattr(factory, "SLACK_CONNECTIONS", SimpleNamespace(entry=lambda _pid: entry))
        monkeypatch.setattr(factory, "_HANDLERS_INSTALLED", set())
        self._app = _BoltApp()
        factory._install_handlers("cp-sl", self._app)
        self.told: list[str] = []
        self._client = SimpleNamespace(
            chat_postEphemeral=AsyncMock(side_effect=lambda **kw: self.told.append(kw["text"])), chat_update=AsyncMock(),
        )
        self._seq = 0

    async def reply(self, text: str) -> None:
        self._seq += 1
        event = {"channel": "C01", "thread_ts": self.anchor, "ts": f"1700000001.{self._seq:06d}", "user": "U7", "text": text}
        await self._app.events["message"](event, self._client, SimpleNamespace())

    async def approve(self) -> None:
        body = {
            "actions": [{"value": f"approve:{WS}:{SID}:{TCID}"}], "channel": {"id": "C01"}, "user": {"id": "U7"},
            "message": {"ts": "1700000000.000200", "blocks": []},
        }
        await self._app.actions["approve"](AsyncMock(), body, self._client)


class _Discord:
    """The Discord handlers on a never-connected discord.Client, an unconnected DiscordChannelAdapter and a thread under its channel."""

    channel_id = "ch-dc"
    anchor = "777"                               # the thread's id

    def __init__(self, monkeypatch, sp, bus) -> None:
        discord = pytest.importorskip("discord")
        from primer.channel.discord import factory
        from primer.channel.discord.adapter import DiscordChannelAdapter

        provider = ChannelProvider(
            id="cp-dc", provider=ChannelProviderType.DISCORD, config=DiscordChannelProviderConfig(bot_token=SecretStr("x" * 60)),
        )
        channel = Channel(id=self.channel_id, provider_id="cp-dc", provider=ChannelProviderType.DISCORD, external_id="500")
        adapter = DiscordChannelAdapter(
            provider=provider, channel=channel, inbox=ChannelInbox(event_bus=bus, storage_provider=sp), storage_provider=sp, event_bus=bus,
        )
        entry = SimpleNamespace(adapters_by_channel_id={"500": adapter})
        monkeypatch.setattr(factory, "DISCORD_CONNECTIONS", SimpleNamespace(entry=lambda _pid: entry))
        monkeypatch.setattr(factory, "_HANDLERS_INSTALLED", set())
        self._client = discord.Client(intents=discord.Intents.none())
        factory._install_handlers("cp-dc", self._client, channel)
        thread_cls = type("_Thread", (discord.Thread,), {})     # a real Thread for the isinstance check, without its gateway __init__
        self._thread = thread_cls.__new__(thread_cls)
        self._thread.id, self._thread.parent_id = int(self.anchor), 500
        self.told: list[str] = []
        self._seq = 0

    def _tell(self, text, **_kw) -> None:
        self.told.append(text)

    async def reply(self, text: str) -> None:
        self._seq += 1
        author = SimpleNamespace(bot=False, id=7, name="dee", display_name="Dee")
        message = SimpleNamespace(
            channel=self._thread, author=author, id=9000 + self._seq, content=text, attachments=[], reply=AsyncMock(side_effect=self._tell),
        )
        await self._client.on_message(message)

    async def approve(self) -> None:
        interaction = SimpleNamespace(
            data={"custom_id": f"approve:{WS}:{SID}:{TCID}"}, channel=SimpleNamespace(parent_id=500), channel_id=int(self.anchor),
            response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock(side_effect=self._tell)),
            edit_original_response=AsyncMock(), message=SimpleNamespace(content="Approve delete_workspace?"), user=SimpleNamespace(id=7),
        )
        await self._client.on_interaction(interaction)


@pytest.fixture
async def sp(tmp_path):
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "threads.sqlite"))
    await provider.initialize()
    yield provider
    await provider.aclose()


@pytest.fixture
async def bus():
    b = _RecordingBus()
    await b.initialize()
    yield b
    await b.aclose()


@pytest.fixture(params=["slack", "discord"])
async def platform(request, monkeypatch, sp, bus):
    return {"slack": _Slack, "discord": _Discord}[request.param](monkeypatch, sp, bus)


def _session(**park) -> WorkspaceSession:
    return WorkspaceSession(
        id=SID, workspace_id=WS, binding=AgentSessionBinding(kind="agent", agent_id="agt"), status=SessionStatus.RUNNING,
        created_at=datetime.now(UTC), initiated_by=ADMIN, **park,
    )


def _parked_on(tool_name: str, resume_metadata: dict) -> WorkspaceSession:
    event_key = f"{'ask_user' if tool_name == 'ask_user' else 'tool_approval'}:{SID}:{TCID}"
    return _session(
        parked_status="parked", parked_at=datetime.now(UTC), parked_event_key=event_key,
        parked_state={"tool_call_id": TCID, "yielded": {"tool_name": tool_name, "event_key": event_key, "resume_metadata": resume_metadata}},
    )


def _asking(gate_id: str) -> WorkspaceSession:
    """Parked on an ask_user question whose gate is ``gate_id``."""
    return _parked_on("ask_user", {"prompt": "Which currency?", "response_schema": None, "tool_call_id": TCID, "files": None, "gate_id": gate_id})


def _awaiting_alice() -> WorkspaceSession:
    """Parked on an approval routed to alice only."""
    return _parked_on("_approval", {
        "original_call": {"id": TCID, "name": "delete_workspace", "arguments": {"id": "ws-x"}},
        "approvers": {"kind": "users", "users": ["alice"]},
    })


async def _map_thread(sp, platform, *, question_gate: str | None = None) -> None:
    """The thread mapped to the session, as a fire's ``_map_thread`` writes it; with ``question_gate``, a question posted into it too, as the
    adapter's ``post_prompt`` upgrades the same row."""
    store = CorrelationStore(sp)
    await store.upsert_thread_session(channel_id=platform.channel_id, anchor=platform.anchor, workspace_id=WS, session_id=SID)
    if question_gate is not None:
        await store.upsert_session(
            channel_id=platform.channel_id, anchor=platform.anchor, workspace_id=WS, session_id=SID, tool_call_id=TCID, gate_id=question_gate,
        )


async def _mapping(sp, platform):
    return await CorrelationStore(sp).lookup(platform.channel_id, platform.anchor)


async def _queued(sp) -> list[str]:
    """The texts waiting in the session's queue for the drain, oldest first."""
    page = await sp.get_storage(PendingSessionMessage).find(
        Q(PendingSessionMessage).where("session_id", SID).build(), OffsetPage(offset=0, length=50),
    )
    rows = sorted(page.items, key=lambda row: row.enqueued_at)
    return ["\n".join(part["text"] for part in row.parts if part.get("type") == "text") for row in rows]


async def _session_ids(sp) -> list[str]:
    return [row.id for row in (await sp.get_storage(WorkspaceSession).list(OffsetPage(offset=0, length=50))).items]


async def test_a_plain_reply_in_a_mapped_thread_steers_its_session_and_the_thread_stays_mapped(platform, sp, bus) -> None:
    """Ruling 1. The session is in a turn, so the steer waits in its queue for the turn's end (the drain realizes it)."""
    await sp.get_storage(WorkspaceSession).create(_session(turn_status="running"))
    await _map_thread(sp, platform)

    await platform.reply("any update?")

    assert bus.keys("ask_user:") == [], "a reply with no open question was published as an answer"
    mapping = await _mapping(sp, platform)
    assert mapping is not None and mapping.session_id == SID, "the thread lost its session"
    assert mapping.tool_call_id is None
    assert await _queued(sp) == ["any update?"], "the reply did not reach the thread's session"

    await platform.reply("and the logs?")

    assert await _queued(sp) == ["any update?", "and the logs?"], "the second reply did not reach the same session"
    assert (await _mapping(sp, platform)).session_id == SID
    assert await _session_ids(sp) == [SID]
    assert bus.keys("ask_user:") == []
    assert platform.told == []


async def test_a_reply_to_an_open_question_answers_it_and_the_next_reply_steers_the_same_session(platform, sp, bus) -> None:
    """Ruling 2. Nothing consumes the answer here, so the session stays parked and the next reply waits in its queue behind the park."""
    await sp.get_storage(WorkspaceSession).create(_asking(GATE))
    await _map_thread(sp, platform, question_gate=GATE)

    await platform.reply("EUR")

    answers = [(key, payload) for key, payload in bus.published if key.startswith("ask_user:")]
    assert [key for key, _ in answers] == [f"ask_user:{SID}:{TCID}"] and answers[0][1]["response"] == "EUR"
    mapping = await _mapping(sp, platform)
    assert mapping is not None and mapping.session_id == SID, "answering the question unmapped the thread"
    assert mapping.tool_call_id is None and mapping.gate_id is None, "the answered question is still open on the thread"
    assert await _queued(sp) == [], "the answer was also queued as a message"

    await platform.reply("thanks, carry on")

    assert bus.keys("ask_user:") == [f"ask_user:{SID}:{TCID}"], "the next reply was taken for an answer again"
    assert await _queued(sp) == ["thanks, carry on"], "the next reply did not reach the same session"
    assert (await _mapping(sp, platform)).session_id == SID
    assert await _session_ids(sp) == [SID]
    assert platform.told == []


async def test_a_reply_to_a_replaced_question_is_refused_and_the_thread_stays_mapped(platform, sp, bus) -> None:
    """Ruling 2, the stale case: the person is told, as before; the dead question is cleared with ``clear_gate``, so the next reply steers."""
    await sp.get_storage(WorkspaceSession).create(_asking(NEWER_GATE))
    await _map_thread(sp, platform, question_gate=GATE)

    await platform.reply("EUR")

    assert platform.told == [QUESTION_STALE_NOTICE]
    assert bus.keys("ask_user:") == []
    mapping = await _mapping(sp, platform)
    assert mapping is not None and mapping.session_id == SID and mapping.tool_call_id is None

    await platform.reply("what is the new question?")

    assert await _queued(sp) == ["what is the new question?"]
    assert bus.keys("ask_user:") == []
    assert (await _mapping(sp, platform)).session_id == SID


async def test_a_restricted_approval_still_refuses_the_platform_user_and_a_reply_still_steers_the_admins_session(platform, sp, bus) -> None:
    """Ruling 3: a gate routed to alice refuses the click of a platform user, while a plain reply in the thread steers the admin's session
    unranked (#880). An approval writes no gate onto the thread's row, so the reply is not mistaken for a decision either."""
    await sp.get_storage(WorkspaceSession).create(_awaiting_alice())
    await _map_thread(sp, platform)

    await platform.approve()

    assert platform.told == [APPROVAL_ROUTED_NOTICE]
    assert bus.keys("tool_approval:") == [], "a platform user decided a gate routed to alice"

    await platform.reply("why is this waiting?")

    assert await _queued(sp) == ["why is this waiting?"], "the reply did not reach the admin's session"
    assert bus.published == []
    assert (await _mapping(sp, platform)).session_id == SID
