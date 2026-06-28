"""E2E: offline channel event -> action mapping (Part B dispatch).

These tests drive a synthetic, normalized
:class:`~primer.model.channel_event.ChannelEvent` straight through the
rule-match path of :class:`~primer.channel.event_dispatch.ChannelEventRouter`
and assert that the seeded ``Subscription`` action ran. They are
provider-agnostic: the event is constructed already-normalized (exactly
the shape Part A's normalizers emit), so no provider gateway or live
socket is involved. Like the sibling in-process journeys
(``test_channel_event_action_inprocess_journey``) the whole thing runs on
a real :class:`SqliteStorageProvider` with the real
:func:`primer.trigger.dispatch.fire_trigger` and the real action
dispatchers - no spies, no HTTP, no LLM, no Postgres.

Two action surfaces are covered:

  1. ``start_chat`` - a ``command.invoked`` event whose
     ``command_name == "deploy"`` matches the binding ``Subscription``'s
     :class:`EventMatcher` and the dispatcher creates a :class:`Chat`
     bound to the source thread, seeding the event text as the first
     ``user_message``. A non-matching event (``command_name="status"``)
     creates NO chat.

  2. ``agent_fresh_session`` - a matching ``command.invoked`` event
     creates a :class:`WorkspaceSession` attributed to the subscription,
     and the session's resolved reply binding equals the subscription's
     ``reply_target`` channel.

The two e2e modules in this iteration deliberately drive the router in
process rather than over HTTP: there is no REST endpoint that injects a
synthetic inbound ``ChannelEvent`` (inbound arrives from a provider
gateway), so the router is the highest-fidelity offline entry point that
still exercises matcher -> dispatch -> action for real. The e2e conftest's
``PRIMER_RUN_E2E`` gate still collect-ignores the module by default.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

# Importing the action dispatcher modules registers them under their kind.
import primer.trigger.subscribers.agent_fresh_session  # noqa: F401
import primer.trigger.subscribers.start_chat  # noqa: F401
from primer.channel.correlation import CorrelationStore
from primer.channel.event_dispatch import ChannelEventRouter
from primer.channel.reply_binding import resolve_reply_binding
from primer.model.agent import Agent, AgentModel
from primer.model.channel import (
    Channel,
    ChannelProviderType,
    TelegramChannelConfig,
)
from primer.model.channel_event import (
    ChannelEvent,
    EventSender,
    NormalizedEventType,
)
from primer.model.chats import Chat, ChatMessage
from primer.model.event_matcher import EventMatcher
from primer.model.provider import SqliteConfig
from primer.model.storage import Op, OffsetPage
from primer.model.trigger import (
    AgentFreshSubConfig,
    ChannelTriggerConfig,
    StartChatSubConfig,
    Subscription,
    Trigger,
)
from primer.model.workspace import (
    Workspace,
    WorkspaceChannelLink,
    WorkspaceRuntimeMeta,
)
from primer.model.workspace_session import WorkspaceSession
from primer.storage.q import Q
from primer.storage.sqlite import SqliteStorageProvider
from primer.trigger.subscribers import DispatchDeps


# ---------------------------------------------------------------------------
# Doubles: a recording bus + a no-op claim engine / scheduler / registry.
# The action dispatchers run for real; only the out-of-process side-effects
# (claim pulse, worker enqueue, workspace slot) are stubbed, exactly as the
# in-process journey relies on.
# ---------------------------------------------------------------------------


class _RecordingBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, event_key, payload=None):
        self.published.append((event_key, payload or {}))


class _FakeClaimEngine:
    async def upsert(self, kind, entity_id, *, priority=100, next_attempt_at=None):
        return None


class _FakeScheduler:
    async def enqueue(self, sid: str) -> None:
        return None


class _NoopWorkspace:
    """Live-workspace stub: start_session is a no-op (the channel flow only
    needs the on-disk slot allocation call to succeed, not to persist)."""

    async def start_session(
        self, binding: Any, *, id: str,
        instructions: Any = None, parent_session_id: Any = None,
    ) -> None:
        return None


class _NoopWorkspaceRegistry:
    def get(self, workspace_id: str) -> Any | None:
        return None

    async def get_workspace(self, workspace_id: str) -> _NoopWorkspace:
        # The session factory (post-2271fa0d) allocates the on-disk session
        # slot via get_workspace(...).start_session(...). Hand back a stub so
        # the agent-fresh dispatcher completes and the session row is created.
        return _NoopWorkspace()


async def _provider(tmp_path: Path) -> SqliteStorageProvider:
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "e2a.sqlite"))
    await p.initialize()
    return p


async def _seed_agent(p) -> Agent:
    agent = Agent(
        id="ag-e2a",
        description="event-to-action agent",
        model=AgentModel(provider_id="prov", model_name="model"),
    )
    await p.get_storage(Agent).create(agent)
    return agent


async def _seed_channel(p) -> Channel:
    ch = Channel(
        id="ch-e2a",
        provider_id="cp-e2a",
        provider=ChannelProviderType.TELEGRAM,
        external_id="555",
        config=TelegramChannelConfig(
            chats={"enabled": False, "default_agent": None},
        ),
    )
    await p.get_storage(Channel).create(ch)
    return ch


def _build_router(p, store, *, bus) -> ChannelEventRouter:
    deps = DispatchDeps(
        storage_provider=p,
        claim_engine=_FakeClaimEngine(),
        scheduler=_FakeScheduler(),
        workspace_registry=_NoopWorkspaceRegistry(),
        event_bus=bus,
    )
    return ChannelEventRouter(
        storage_provider=p,
        correlation_store=store,
        fire_deps=deps,
        event_bus=bus,
    )


def _command_event(
    *, channel_id: str, command_name: str, text: str,
    thread_anchor: str | None, surface: str, event_id: str,
) -> ChannelEvent:
    return ChannelEvent(
        provider=ChannelProviderType.TELEGRAM,
        provider_id="cp-e2a",
        event_id=event_id,
        type=NormalizedEventType.COMMAND_INVOKED,
        occurred_at=datetime.now(timezone.utc),
        room_external_id="555",
        channel_id=channel_id,
        surface=surface,
        thread_anchor=thread_anchor,
        sender=EventSender(external_id="u-1", display_name="Cara"),
        text=text,
        command={"name": command_name, "args": ""},
    )


# ===========================================================================
# (1) command.invoked -> start_chat: chat bound to source thread + seeded
# ===========================================================================


@pytest.mark.asyncio
async def test_command_event_starts_bound_chat(tmp_path: Path) -> None:
    """A ``command.invoked`` event matching the binding's
    ``EventMatcher`` (command_name == "deploy") fires the channel
    trigger; the ``start_chat`` dispatcher creates a Chat bound to the
    source ``(channel_id, thread_anchor)`` and seeds the event text as
    the first user_message. A non-matching ``status`` command creates no
    chat.
    """
    p = await _provider(tmp_path)
    agent = await _seed_agent(p)
    ch = await _seed_channel(p)
    store = CorrelationStore(p)

    await p.get_storage(Trigger).create(Trigger(
        id="trg-e2a", slug="evt-start-chat", name="start-chat rule",
        config=ChannelTriggerConfig(provider_id="cp-e2a", channel_id=ch.id),
        created_at=datetime.now(timezone.utc)))
    await p.get_storage(Subscription).create(Subscription(
        id="sub-e2a", trigger_id="trg-e2a",
        config=StartChatSubConfig(agent_id=agent.id),
        payload_template="{{ event.text }}",
        event_matcher=EventMatcher(
            event_type=NormalizedEventType.COMMAND_INVOKED,
            command_name="deploy",
        ),
        parallelism="queue",
        enabled=True,
        created_at=datetime.now(timezone.utc)))

    bus = _RecordingBus()
    router = _build_router(p, store, bus=bus)

    # ----- matching command -> one bound chat with the seeded text -----
    await router.route_event(
        event=_command_event(
            channel_id=ch.id, command_name="deploy", text="deploy now",
            thread_anchor="thr-e2a", surface="thread", event_id="ev-deploy",
        ),
        channel=ch,
    )

    chats = (await p.get_storage(Chat).find(
        None, OffsetPage(offset=0, length=50),
    )).items
    assert len(chats) == 1, f"expected exactly one chat; got {len(chats)}"
    chat = chats[0]
    # (a) bound to the source thread.
    assert chat.channel_binding is not None
    assert chat.channel_binding.channel_id == ch.id
    assert chat.channel_binding.thread_external_id == "thr-e2a"
    assert chat.agent_id == agent.id

    # (b) the firing event's text seeded the first user_message.
    msgs = (await p.get_storage(ChatMessage).find(
        Q(ChatMessage).where_op("chat_id", Op.EQ, chat.id).build(),
        OffsetPage(offset=0, length=50),
    )).items
    user_msgs = [m for m in msgs if m.kind == "user_message"]
    assert len(user_msgs) == 1, f"expected one seeded message; got {user_msgs}"
    assert "deploy now" in str(user_msgs[0].payload)

    # (c) a non-matching command creates NO chat.
    await router.route_event(
        event=_command_event(
            channel_id=ch.id, command_name="status", text="status",
            thread_anchor="thr-e2a-2", surface="thread",
            event_id="ev-status",
        ),
        channel=ch,
    )
    chats_after = (await p.get_storage(Chat).find(
        None, OffsetPage(offset=0, length=50),
    )).items
    assert len(chats_after) == 1, (
        "a non-matching command must not create a chat; "
        f"got {len(chats_after)} chats"
    )


# ===========================================================================
# (2) command.invoked -> agent_fresh_session: session + reply binding
# ===========================================================================


@pytest.mark.asyncio
async def test_command_event_starts_session_with_reply_binding(
    tmp_path: Path,
) -> None:
    """A matching ``command.invoked`` event fires an
    ``agent_fresh_session`` binding; a session is created attributed to
    the subscription and its resolved reply binding names the
    subscription's ``reply_target`` channel.
    """
    p = await _provider(tmp_path)
    agent = await _seed_agent(p)
    ch = await _seed_channel(p)
    store = CorrelationStore(p)

    # The workspace carries a standing reply binding to the source channel;
    # the subscription's reply_target names the same channel. The created
    # session's resolved reply binding must agree with that target.
    ws = Workspace(
        id="ws-e2a",
        description="event-to-action workspace",
        template_id="t-e2a",
        provider_id="p-e2a",
        created_at=datetime.now(timezone.utc),
        runtime_meta=WorkspaceRuntimeMeta(
            url="ws://127.0.0.1:5959/", token=SecretStr("t"),
        ),
        reply_binding=WorkspaceChannelLink(channel_id=ch.id),
    )
    await p.get_storage(Workspace).create(ws)

    await p.get_storage(Trigger).create(Trigger(
        id="trg-e2a-sess", slug="evt-fresh-session", name="fresh-session rule",
        config=ChannelTriggerConfig(provider_id="cp-e2a", channel_id=ch.id),
        created_at=datetime.now(timezone.utc)))
    await p.get_storage(Subscription).create(Subscription(
        id="sub-e2a-sess", trigger_id="trg-e2a-sess",
        config=AgentFreshSubConfig(workspace_id=ws.id, agent_id=agent.id),
        payload_template="{{ event.text }}",
        event_matcher=EventMatcher(
            event_type=NormalizedEventType.COMMAND_INVOKED,
            command_name="deploy",
        ),
        reply_target={"channel_id": ch.id},
        parallelism="queue",
        enabled=True,
        created_at=datetime.now(timezone.utc)))

    bus = _RecordingBus()
    router = _build_router(p, store, bus=bus)

    await router.route_event(
        event=_command_event(
            channel_id=ch.id, command_name="deploy", text="run a check",
            thread_anchor=None, surface="channel", event_id="ev-sess",
        ),
        channel=ch,
    )

    sessions = p.get_storage(WorkspaceSession)
    page = await sessions.find(
        Q(WorkspaceSession)
        .where_op("metadata.subscription_id", Op.EQ, "sub-e2a-sess").build(),
        OffsetPage(offset=0, length=50),
    )
    assert len(page.items) == 1, (
        f"expected one session for the fired binding; got {len(page.items)}"
    )
    session = page.items[0]

    sub = await p.get_storage(Subscription).get("sub-e2a-sess")
    binding = await resolve_reply_binding(session, storage_provider=p)
    assert binding is not None, "session must resolve a reply binding"
    assert binding.channel_id == sub.reply_target["channel_id"], (
        "the session's reply binding must name the subscription's "
        f"reply_target channel; got {binding.channel_id!r} vs "
        f"{sub.reply_target['channel_id']!r}"
    )
