"""start_chat dispatcher — Spec §6.4, §14 decision 2."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.int.claim import ClaimKind
from primer.model.channel import ChannelProviderType
from primer.model.channel_event import (
    ChannelEvent,
    EventSender,
    NormalizedEventType,
)
from primer.model.chats import Chat, ChatMessage
from primer.model.storage import OffsetPage
from primer.model.trigger import StartChatSubConfig, Subscription, SubscriptionKind
from primer.trigger.subscribers import DispatchDeps


def _make_event(text: str = "hello there") -> ChannelEvent:
    return ChannelEvent(
        provider=ChannelProviderType.SLACK,
        provider_id="p-1",
        event_id="ev-1",
        type=NormalizedEventType.MESSAGE_POSTED,
        occurred_at=datetime.now(timezone.utc),
        channel_id="channel-1",
        surface="thread",
        thread_anchor="thr-1",
        sender=EventSender(external_id="u-1"),
        text=text,
    )


def _make_sub(agent_id: str) -> Subscription:
    return Subscription(
        id="sb-sc",
        trigger_id="tr-c",
        config=StartChatSubConfig(agent_id=agent_id),
        created_at=datetime.now(timezone.utc),
    )


def test_start_chat_kind_and_config():
    assert SubscriptionKind.START_CHAT.value == "start_chat"
    assert StartChatSubConfig(agent_id="ag-1").kind == "start_chat"
    sub = Subscription(
        id="sb-sc",
        trigger_id="tr-c",
        config=StartChatSubConfig(agent_id="ag-1"),
        created_at=datetime.now(timezone.utc),
    )
    assert sub.config.agent_id == "ag-1"


@pytest.mark.asyncio
async def test_start_chat_creates_bound_chat_and_seeds(
    fake_storage_provider, fake_claim_engine, fake_scheduler, seeded_agent,
):
    from primer.trigger.subscribers.start_chat import StartChatDispatcher

    ev = _make_event("hello there")
    sub = _make_sub(seeded_agent.id)
    deps = DispatchDeps(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        scheduler=fake_scheduler,
    )
    res = await StartChatDispatcher().dispatch(
        sub,
        rendered_payload="hello there",
        fire_context={"event": ev.model_dump(mode="json"), "trigger_id": "tr-c"},
        fire_id="fire-1",
        deps=deps,
    )
    assert res.ok is True
    assert res.artefact_id is not None

    chats = fake_storage_provider.get_storage(Chat)
    chat = await chats.get(res.artefact_id)
    assert chat.agent_id == seeded_agent.id
    assert chat.channel_binding.channel_id == "channel-1"
    assert chat.channel_binding.thread_external_id == "thr-1"
    assert chat.turn_status == "claimable"
    assert chat.last_seq == 1

    messages = fake_storage_provider.get_storage(ChatMessage)
    page = await messages.list(OffsetPage(offset=0, length=10))
    user_msgs = [m for m in page.items if m.kind == "user_message"]
    assert len(user_msgs) == 1
    assert user_msgs[0].payload["content"] == "hello there"
    assert user_msgs[0].payload["trigger"]["fire_id"] == "fire-1"

    assert (ClaimKind.CHAT, chat.id, 10) in fake_claim_engine.upserts


@pytest.mark.asyncio
async def test_start_chat_missing_agent(
    fake_storage_provider, fake_claim_engine, fake_scheduler,
):
    from primer.trigger.subscribers.start_chat import StartChatDispatcher

    ev = _make_event()
    deps = DispatchDeps(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        scheduler=fake_scheduler,
    )
    res = await StartChatDispatcher().dispatch(
        _make_sub("ag-missing"),
        rendered_payload="hello there",
        fire_context={"event": ev.model_dump(mode="json"), "trigger_id": "tr-c"},
        fire_id="fire-1",
        deps=deps,
    )
    assert res.ok is False
    assert res.error_code == "agent_not_found"

    chats = fake_storage_provider.get_storage(Chat)
    page = await chats.list(OffsetPage(offset=0, length=10))
    assert page.items == []


@pytest.mark.asyncio
async def test_start_chat_no_event_in_context(
    fake_storage_provider, fake_claim_engine, fake_scheduler, seeded_agent,
):
    from primer.trigger.subscribers.start_chat import StartChatDispatcher

    deps = DispatchDeps(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        scheduler=fake_scheduler,
    )
    res = await StartChatDispatcher().dispatch(
        _make_sub(seeded_agent.id),
        rendered_payload="hello there",
        fire_context={},
        fire_id="fire-1",
        deps=deps,
    )
    assert res.ok is False
    assert res.error_code == "no_event"
