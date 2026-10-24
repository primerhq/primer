"""Discord factory handler-registration and channel-event routing tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

discord = pytest.importorskip("discord")

from pydantic import SecretStr

from primer.channel.discord.factory import _install_handlers, _route_channel_event
from primer.model.channel import (
    Channel, ChannelProviderType, DiscordChannelProviderConfig,  # noqa: F401
)


class _FakeClient:
    """Bare object that accepts attribute assignment, like discord.Client."""

    def is_ready(self) -> bool:
        # Not yet ready: the already-ready sync path is exercised separately.
        return False


def _channel() -> Channel:
    return Channel(id="ch-1", provider_id="cp-discord",
                   provider=ChannelProviderType.DISCORD, external_id="9001")


def test_handlers_bound_to_real_gateway_event_names():
    # Regression: the base discord.Client dispatches by looking up
    # self.on_<event>; it has no add_listener, and client.event would bind the
    # _on_interaction/_on_message coroutines under the wrong attribute. The
    # handlers must land on on_interaction / on_message so clicks and thread
    # replies actually dispatch.
    client = _FakeClient()
    _install_handlers("dc-prov-handler-test", client, _channel())
    assert asyncio.iscoroutinefunction(getattr(client, "on_interaction", None))
    assert asyncio.iscoroutinefunction(getattr(client, "on_message", None))


# --------------------------------------------------------------------------- #
# _route_channel_event: mentions_bot and the sender's role ids
# --------------------------------------------------------------------------- #
#
# The factory derives ``mentions_bot`` (the bot user mentioned directly) and
# the sender's guild role ids from the SDK objects; the real normalizer runs
# and the router double captures the normalized event.

BOT_ID = 9001
GUILD_ID = 2002
OTHER_ID = 3003


class _EventCapturingRouter:
    def __init__(self) -> None:
        self.events: list = []

    async def route_event(self, *, event, channel, media_parts=None):
        self.events.append(event)


def _adapter(router, *, has_client: bool = True):
    client = (
        SimpleNamespace(user=SimpleNamespace(id=BOT_ID)) if has_client else None
    )
    return SimpleNamespace(
        _inbound_router=lambda: router,
        _channel=_channel(),
        _client=client,
        collect_inbound_media=AsyncMock(return_value=[]),
    )


def _guild_message(*, mentions=(), author_roles=(), author_id=42):
    author = SimpleNamespace(
        id=author_id, name="dee", display_name="Dee", bot=False,
        roles=[SimpleNamespace(id=r) for r in author_roles],
    )
    return SimpleNamespace(
        id=1,
        content="hello",
        channel=SimpleNamespace(id=555),
        author=author,
        mentions=[SimpleNamespace(id=m) for m in mentions],
        guild=SimpleNamespace(id=GUILD_ID),
    )


def _dm_message() -> SimpleNamespace:
    # A DM sender is a bare discord.User: no guild, no roles attribute.
    channel_cls = type("_FakeDMChannel", (discord.DMChannel,), {})
    channel = channel_cls.__new__(channel_cls)
    channel.id = 321
    author = SimpleNamespace(id=42, name="dee", display_name="Dee", bot=False)
    return SimpleNamespace(
        id=2,
        content="dm",
        channel=channel,
        author=author,
        mentions=[],
        guild=None,
    )


@pytest.mark.asyncio
async def test_a_message_that_mentions_the_bot_sets_mentions_bot():
    router = _EventCapturingRouter()
    adapter = _adapter(router)
    dispatched = await _route_channel_event(
        adapter, "cp-discord", _guild_message(mentions=(OTHER_ID, BOT_ID)),
    )
    assert dispatched is True
    assert len(router.events) == 1
    assert router.events[0].mentions_bot is True


@pytest.mark.asyncio
async def test_a_message_that_mentions_only_another_user_does_not():
    router = _EventCapturingRouter()
    adapter = _adapter(router)
    dispatched = await _route_channel_event(
        adapter, "cp-discord", _guild_message(mentions=(OTHER_ID,)),
    )
    assert dispatched is True
    assert router.events[0].mentions_bot is False


@pytest.mark.asyncio
async def test_an_everyone_mention_does_not_count_as_the_bot():
    # @everyone / @here and role mentions never appear in discord's
    # ``message.mentions`` at all (they live in mention_everyone /
    # mention_roles), so a message with only @everyone carries no user
    # mention; and an entry carrying the @everyone role's id (the guild's
    # own id) must not equal the bot user's id.
    router = _EventCapturingRouter()
    adapter = _adapter(router)
    dispatched = await _route_channel_event(
        adapter, "cp-discord", _guild_message(mentions=(GUILD_ID,)),
    )
    assert dispatched is True
    assert router.events[0].mentions_bot is False


@pytest.mark.asyncio
async def test_no_client_or_bot_user_means_no_mentions_bot():
    router = _EventCapturingRouter()
    adapter = _adapter(router, has_client=False)
    dispatched = await _route_channel_event(
        adapter, "cp-discord", _guild_message(mentions=(BOT_ID,)),
    )
    assert dispatched is True
    assert router.events[0].mentions_bot is False


@pytest.mark.asyncio
async def test_the_sender_carries_their_role_ids_without_everyone():
    # Two real roles plus @everyone (whose id is the guild's own id): the
    # event carries the two role ids only, as strings.
    router = _EventCapturingRouter()
    adapter = _adapter(router)
    dispatched = await _route_channel_event(
        adapter, "cp-discord",
        _guild_message(author_roles=(111, 222, GUILD_ID)),
    )
    assert dispatched is True
    assert router.events[0].sender.roles == ["111", "222"]


@pytest.mark.asyncio
async def test_a_dm_sender_carries_no_roles():
    router = _EventCapturingRouter()
    adapter = _adapter(router)
    dispatched = await _route_channel_event(
        adapter, "cp-discord", _dm_message(),
    )
    assert dispatched is True
    assert router.events[0].surface == "dm"
    assert router.events[0].sender.roles == []


# --------------------------------------------------------------------------- #
# End-to-end: a binding matched on mentions_bot fires for the mentioning
# message only
# --------------------------------------------------------------------------- #
#
# Drives the real ChannelEventRouter + the real fire_trigger over the
# top-level fake_storage_provider: the router fixture pattern of
# tests/channel/test_event_dispatch.py, with the session_append steer
# fixture of tests/trigger/test_dispatch_event_matcher.py (a queued steer
# behind an open turn, whose observable is the PendingSessionMessage row).


class _FakeScheduler:
    def __init__(self) -> None:
        self.enqueued: list[str] = []

    async def enqueue(self, sid: str) -> None:
        self.enqueued.append(sid)


class _FakeClaimEngine:
    def __init__(self) -> None:
        self.upserts: list = []

    async def upsert(self, kind, entity_id, *, priority=100, next_attempt_at=None):
        self.upserts.append((kind, entity_id, priority))


class _FakeBus:
    def __init__(self) -> None:
        self.published: list = []

    async def publish(self, event_key, payload=None):
        self.published.append((event_key, payload or {}))


@pytest.mark.asyncio
async def test_a_binding_on_mentions_bot_fires_only_for_the_mentioning_message(
    fake_storage_provider,
):
    from primer.channel.correlation import CorrelationStore
    from primer.channel.event_dispatch import ChannelEventRouter
    from primer.model.agent import Agent, AgentModel
    from primer.model.channel_event import NormalizedEventType
    from primer.model.event_matcher import EventMatcher
    from primer.model.trigger import (
        ChannelTriggerConfig,
        SessionAppendSubConfig,
        Subscription,
        Trigger,
    )
    from primer.model.workspace_session import (
        AgentSessionBinding,
        PendingSessionMessage,
        SessionStatus,
        WorkspaceSession,
    )
    from primer.trigger.subscribers import DispatchDeps

    sp = fake_storage_provider
    now = datetime.now(timezone.utc)

    ch = _channel()
    await sp.get_storage(Channel).create(ch)
    await sp.get_storage(Agent).create(Agent(
        id="ag-1", description="seeded test agent",
        model=AgentModel(profile_id="p--m"),
    ))
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id="cn-1", workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="ag-1"),
        status=SessionStatus.RUNNING, turn_status="running",
        created_at=now,
    ))
    await sp.get_storage(Trigger).create(Trigger(
        id="trg-d1", slug="mentions-bot", name="mentions binding",
        config=ChannelTriggerConfig(provider_id="cp-discord", channel_id=ch.id),
        created_at=now,
    ))
    await sp.get_storage(Subscription).create(Subscription(
        id="sb-mentions", trigger_id="trg-d1",
        config=SessionAppendSubConfig(session_id="cn-1"),
        payload_template="hi",
        parallelism="queue",
        event_matcher=EventMatcher(
            event_type=NormalizedEventType.MESSAGE_POSTED, mentions_bot=True,
        ),
        enabled=True,
        created_at=now,
    ))

    deps = DispatchDeps(
        storage_provider=sp, claim_engine=_FakeClaimEngine(),
        scheduler=_FakeScheduler(), workspace_registry=object(),
    )
    router = ChannelEventRouter(
        storage_provider=sp, correlation_store=CorrelationStore(sp),
        fire_deps=deps, event_bus=_FakeBus(),
    )
    adapter = SimpleNamespace(
        _inbound_router=lambda: router,
        _channel=ch,
        _client=SimpleNamespace(user=SimpleNamespace(id=BOT_ID)),
        collect_inbound_media=AsyncMock(return_value=[]),
    )

    pending = sp.get_storage(PendingSessionMessage)._data

    # The message that mentions the bot: the binding matches, the steer is
    # queued behind the open turn.
    assert await _route_channel_event(
        adapter, "cp-discord", _guild_message(mentions=(BOT_ID,)),
    )
    assert len(pending) == 1
    row = next(iter(pending.values()))
    assert row.session_id == "cn-1"
    assert row.parts == [{"type": "text", "text": "hi"}]

    # The message that mentions someone else: the matcher refuses, nothing
    # new is queued.
    assert await _route_channel_event(
        adapter, "cp-discord", _guild_message(mentions=(OTHER_ID,)),
    )
    assert len(pending) == 1
