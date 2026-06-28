"""E2E: channel-event -> action mapping in-process journey.

Mirrors the ``test_channels_null_adapter_inprocess_journey`` pattern but
drives the NEW normalized-event path end to end, offline (no HTTP, no LLM,
no real network, no Postgres):

  1. A fresh ``command.invoked`` :class:`ChannelEvent` with no correlation
     routes through :meth:`ChannelEventRouter.route_event`, which resolves
     the ``kind="channel"`` :class:`Trigger`, fires it, and the
     ``agent_fresh_session`` subscriber (gated by an ``EventMatcher`` for
     ``command.invoked`` + ``command_name="run"``) creates a session. We
     assert a session row now exists attributed to that subscription.

  2. A ``message.posted`` event whose ``thread_anchor`` matches a seeded
     ``kind="session"`` :class:`ChannelCorrelation` resumes the parked gate
     (publishes ``ask_user:{sid}:{tcid}`` onto the bus) and fires NO
     trigger - correlation wins over rules.

The real :func:`fire_trigger` is used (no spy): the matcher-aware dispatch
loop and the agent-fresh dispatcher run for real against a SqliteStorage
backbone. The workspace registry is a no-op double (the in-memory
fresh-session create path tolerates a ``None`` slot, exactly as
``tests/trigger/test_subscribers_fresh_session.py`` relies on).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

# Importing the subscriber module registers its dispatcher under
# "agent_fresh_session"; fire_trigger also imports it, but be explicit.
import primer.trigger.subscribers.agent_fresh_session  # noqa: F401
from primer.channel.correlation import CorrelationStore
from primer.channel.event_dispatch import ChannelEventRouter
from primer.int.claim import ClaimKind
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
from primer.model.event_matcher import EventMatcher
from primer.model.provider import SqliteConfig
from primer.model.storage import Op, OffsetPage
from primer.model.trigger import (
    AgentFreshSubConfig,
    ChannelTriggerConfig,
    Subscription,
    Trigger,
)
from primer.model.workspace import Workspace, WorkspaceRuntimeMeta
from primer.model.workspace_session import WorkspaceSession
from primer.storage.q import Q
from primer.storage.sqlite import SqliteStorageProvider
from primer.trigger.subscribers import DispatchDeps


class _RecordingBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, event_key, payload=None):
        self.published.append((event_key, payload or {}))


class _FakeClaimEngine:
    def __init__(self) -> None:
        self.upserts: list[tuple[ClaimKind, str, int]] = []

    async def upsert(
        self, kind, entity_id, *, priority=100, next_attempt_at=None,
    ) -> None:
        self.upserts.append((kind, entity_id, priority))


class _FakeScheduler:
    def __init__(self) -> None:
        self.enqueued: list[str] = []

    async def enqueue(self, sid: str) -> None:
        self.enqueued.append(sid)


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
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "evj.sqlite"))
    await p.initialize()
    return p


async def _seed_agent(p) -> Agent:
    agent = Agent(
        id="ag-evj",
        description="journey agent",
        model=AgentModel(provider_id="prov", model_name="model"),
    )
    await p.get_storage(Agent).create(agent)
    return agent


async def _seed_workspace(p) -> Workspace:
    ws = Workspace(
        id="ws-evj",
        description="journey workspace",
        template_id="t-evj",
        provider_id="p-evj",
        created_at=datetime.now(timezone.utc),
        runtime_meta=WorkspaceRuntimeMeta(
            url="ws://127.0.0.1:5959/", token=SecretStr("t"),
        ),
    )
    await p.get_storage(Workspace).create(ws)
    return ws


async def _seed_channel(p) -> Channel:
    ch = Channel(
        id="ch-evj",
        provider_id="cp-evj",
        provider=ChannelProviderType.TELEGRAM,
        external_id="555",
        config=TelegramChannelConfig(
            chats={"enabled": False, "default_agent": None},
        ),
    )
    await p.get_storage(Channel).create(ch)
    return ch


@pytest.mark.asyncio
async def test_channel_event_action_journey(tmp_path: Path) -> None:
    p = await _provider(tmp_path)
    agent = await _seed_agent(p)
    ws = await _seed_workspace(p)
    ch = await _seed_channel(p)
    store = CorrelationStore(p)

    # A channel trigger anchoring an agent_fresh_session subscription gated
    # on command.invoked + command_name == "run".
    await p.get_storage(Trigger).create(Trigger(
        id="trg-evj", slug="evj", name="journey rule",
        config=ChannelTriggerConfig(provider_id="cp-evj", channel_id=ch.id),
        created_at=datetime.now(timezone.utc)))
    await p.get_storage(Subscription).create(Subscription(
        id="sub-evj", trigger_id="trg-evj",
        config=AgentFreshSubConfig(workspace_id=ws.id, agent_id=agent.id),
        event_matcher=EventMatcher(
            event_type=NormalizedEventType.COMMAND_INVOKED,
            command_name="run",
        ),
        parallelism="queue",
        enabled=True,
        created_at=datetime.now(timezone.utc)))

    bus = _RecordingBus()
    deps = DispatchDeps(
        storage_provider=p,
        claim_engine=_FakeClaimEngine(),
        scheduler=_FakeScheduler(),
        workspace_registry=_NoopWorkspaceRegistry(),
        event_bus=bus,
    )
    router = ChannelEventRouter(
        storage_provider=p, correlation_store=store,
        fire_deps=deps, event_bus=bus)

    # ----- (1) Fresh command.invoked -> rule fires -> session created ----
    cmd_event = ChannelEvent(
        provider=ChannelProviderType.TELEGRAM,
        provider_id="cp-evj",
        event_id="ev-cmd",
        type=NormalizedEventType.COMMAND_INVOKED,
        occurred_at=datetime.now(timezone.utc),
        room_external_id="555",
        channel_id=ch.id,
        surface="channel",
        thread_anchor=None,
        sender=EventSender(external_id="u-1", display_name="Cara"),
        text="run a check",
        command={"name": "run", "args": "a check"},
    )
    await router.route_event(event=cmd_event, channel=ch)

    # The agent_fresh_session dispatcher created a session attributed to the
    # subscription. Correlation path was not taken, so no bus publish yet.
    sessions = p.get_storage(WorkspaceSession)
    page = await sessions.find(
        Q(WorkspaceSession)
        .where_op("metadata.subscription_id", Op.EQ, "sub-evj").build(),
        OffsetPage(offset=0, length=50),
    )
    assert len(page.items) == 1, (
        f"expected one session created by the fired channel trigger; "
        f"got {len(page.items)}"
    )
    assert bus.published == []

    # ----- (2) Correlated message.posted -> resume gate, NO trigger fire --
    await store.upsert_session(
        channel_id=ch.id, anchor="thr-evj", workspace_id=ws.id,
        session_id="sess-evj", tool_call_id="tc-evj")

    msg_event = ChannelEvent(
        provider=ChannelProviderType.TELEGRAM,
        provider_id="cp-evj",
        event_id="ev-msg",
        type=NormalizedEventType.MESSAGE_POSTED,
        occurred_at=datetime.now(timezone.utc),
        room_external_id="555",
        channel_id=ch.id,
        surface="thread",
        thread_anchor="thr-evj",
        sender=EventSender(external_id="u-1", display_name="Cara"),
        text="go ahead",
    )
    await router.route_event(event=msg_event, channel=ch)

    assert bus.published == [("ask_user:sess-evj:tc-evj", {"response": "go ahead"})]
    # No new session was created by the correlated reply.
    page2 = await sessions.find(
        Q(WorkspaceSession)
        .where_op("metadata.subscription_id", Op.EQ, "sub-evj").build(),
        OffsetPage(offset=0, length=50),
    )
    assert len(page2.items) == 1
