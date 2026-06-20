"""fire_trigger evaluates Subscription.event_matcher against the fire event."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.model.channel import ChannelProviderType
from primer.model.channel_event import (
    ChannelEvent,
    EventSender,
    NormalizedEventType,
)
from primer.model.chats import Chat
from primer.model.event_matcher import EventMatcher
from primer.model.trigger import (
    ChannelTriggerConfig,
    ChatMessageSubConfig,
    Subscription,
    Trigger,
)
from primer.trigger.dispatch import fire_trigger
from primer.trigger.subscribers import DispatchDeps


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_matching_sub_dispatches_non_matching_skipped(
    fake_storage_provider, fake_claim_engine, fake_scheduler, seeded_agent,
):
    triggers = fake_storage_provider.get_storage(Trigger)
    subs = fake_storage_provider.get_storage(Subscription)
    chats = fake_storage_provider.get_storage(Chat)

    t = Trigger(
        id="tr-c", slug="tr-c", name="channel", description=None,
        config=ChannelTriggerConfig(provider_id="channel-provider-1"),
        enabled=True,
        next_fire_at=None,
        created_at=_now(),
    )
    await triggers.create(t)

    chat = Chat(
        id="cn-1", agent_id=seeded_agent.id, last_seq=0,
        status="active", turn_status="idle",
        created_at=_now(),
    )
    await chats.create(chat)

    sub_open = Subscription(
        id="sb-open", trigger_id="tr-c",
        config=ChatMessageSubConfig(chat_id="cn-1"),
        payload_template="hi",
        event_matcher=EventMatcher(event_type=NormalizedEventType.MESSAGE_POSTED),
        enabled=True,
        created_at=_now(),
    )
    sub_cmd = Subscription(
        id="sb-cmd", trigger_id="tr-c",
        config=ChatMessageSubConfig(chat_id="cn-1"),
        payload_template="hi",
        event_matcher=EventMatcher(
            event_type=NormalizedEventType.MESSAGE_POSTED, command_name="deploy",
        ),
        enabled=True,
        created_at=_now(),
    )
    await subs.create(sub_open)
    await subs.create(sub_cmd)

    ev = ChannelEvent(
        provider=ChannelProviderType.SLACK,
        provider_id="channel-provider-1",
        event_id="ev-1",
        type=NormalizedEventType.MESSAGE_POSTED,
        occurred_at=_now(),
        room_external_id="C123",
        surface="channel",
        message_id="m-1",
        sender=EventSender(external_id="u-1"),
        text="hello",
        command=None,
    )

    deps = DispatchDeps(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        scheduler=fake_scheduler,
    )
    res = await fire_trigger(
        trigger_id="tr-c", scheduled_for=None, deps=deps,
        extra_context={"event": ev.model_dump(mode="json")},
    )
    assert res.skipped is False

    by_id = {r["subscription_id"]: r for r in res.results}

    assert by_id["sb-open"]["ok"] is True
    assert by_id["sb-open"]["skipped"] is False

    assert by_id["sb-cmd"]["ok"] is True
    assert by_id["sb-cmd"]["skipped"] is True
    assert by_id["sb-cmd"]["error_code"] == "skipped_no_match"
