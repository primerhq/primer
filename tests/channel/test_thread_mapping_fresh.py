"""S6 P3: a new thread fires the channel trigger and binds the session.

Spec: docs/superpowers/ux-revamp/10-s6-design.md section 5.
"""

from __future__ import annotations

from datetime import UTC, datetime

from primer.channel.correlation import CorrelationStore
from primer.channel.event_dispatch import ChannelEventRouter
from primer.channel.reply_binding import SESSION_REPLY_BINDING_KEY
from primer.model.channel import Channel, ChannelProviderType, TelegramChannelConfig
from primer.model.channel_event import (
    ChannelEvent,
    EventSender,
    NormalizedEventType,
)
from primer.model.envelope import RELAY_EVERY_TURN_KEY
from primer.model.trigger import ChannelTriggerConfig, Trigger
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.trigger.dispatch import FireResult
from primer.trigger.subscribers import DispatchDeps
from tests.conftest import _FakeStorageProvider


def _channel() -> Channel:
    return Channel(
        id="ch-1", provider_id="cp-1",
        provider=ChannelProviderType.TELEGRAM, external_id="777",
        config=TelegramChannelConfig(),
    )


def _event() -> ChannelEvent:
    return ChannelEvent(
        provider=ChannelProviderType.TELEGRAM, provider_id="cp-1",
        event_id="ev-3", type=NormalizedEventType.MESSAGE_POSTED,
        occurred_at=datetime.now(UTC), channel_id="ch-1", surface="thread",
        thread_anchor="thr-new", sender=EventSender(external_id="u1"),
        text="start something",
    )


def _dm_event() -> ChannelEvent:
    return ChannelEvent(
        provider=ChannelProviderType.TELEGRAM, provider_id="cp-1",
        event_id="ev-4", type=NormalizedEventType.MESSAGE_POSTED,
        occurred_at=datetime.now(UTC), channel_id="ch-1", surface="dm",
        thread_anchor=None, sender=EventSender(external_id="u9"),
        text="start something",
    )


async def _seed(sp, *, interactive: bool):
    await sp.get_storage(Trigger).create(Trigger(
        id="tr-1", slug="ch-trigger", name="Channel",
        config=ChannelTriggerConfig(
            provider_id="cp-1", channel_id="ch-1", interactive=interactive,
        ),
        created_at=datetime.now(UTC),
    ))
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id="s-new", workspace_id="w1",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.RUNNING, created_at=datetime.now(UTC),
    ))


async def _fire_once(sp):
    async def _fake_fire(*, trigger_id, scheduled_for, deps, extra_context=None):
        return FireResult(
            fire_id="fire-1",
            results=[{"ok": True, "skipped": False, "artefact_id": "s-new"}],
        )

    return ChannelEventRouter(
        storage_provider=sp,
        correlation_store=CorrelationStore(sp),
        fire_deps=DispatchDeps(storage_provider=sp, claim_engine=None),
        event_bus=None,
        fire_trigger=_fake_fire,
    )


async def test_fresh_thread_maps_and_stamps_the_reply_binding():
    sp = _FakeStorageProvider()
    await _seed(sp, interactive=True)
    outcome = await (await _fire_once(sp)).route_event(
        event=_event(), channel=_channel(),
    )

    assert outcome.kind == "fired"
    assert outcome.session_id == "s-new"

    record = await CorrelationStore(sp).lookup("ch-1", "thr-new")
    assert record is not None
    assert record.session_id == "s-new"
    assert record.tool_call_id is None

    row = await sp.get_storage(WorkspaceSession).get("s-new")
    assert row.metadata[SESSION_REPLY_BINDING_KEY] == {
        "channel_id": "ch-1", "anchor": "thr-new", "quiet": False,
    }
    assert row.metadata[RELAY_EVERY_TURN_KEY] is True


async def test_non_interactive_trigger_ingests_silently():
    """S6 section 4: interactive=false suppresses the relay entirely."""
    sp = _FakeStorageProvider()
    await _seed(sp, interactive=False)
    await (await _fire_once(sp)).route_event(
        event=_event(), channel=_channel(),
    )
    row = await sp.get_storage(WorkspaceSession).get("s-new")
    assert RELAY_EVERY_TURN_KEY not in row.metadata
    assert row.metadata[SESSION_REPLY_BINDING_KEY]["quiet"] is True


async def test_a_dm_maps_on_its_dm_anchor_with_no_thread_reply_anchor():
    """S6 section 5: the DM anchor is a thread for MAPPING purposes only.

    The reply binding keeps anchor=None so the adapter posts to the DM
    channel root instead of trying to resolve 'dm:u9' as a platform thread.
    """
    sp = _FakeStorageProvider()
    await _seed(sp, interactive=True)
    outcome = await (await _fire_once(sp)).route_event(
        event=_dm_event(), channel=_channel(),
    )
    assert outcome.kind == "fired"
    record = await CorrelationStore(sp).lookup("ch-1", "dm:u9")
    assert record is not None
    assert record.session_id == "s-new"
    row = await sp.get_storage(WorkspaceSession).get("s-new")
    assert row.metadata[SESSION_REPLY_BINDING_KEY]["anchor"] is None
