"""chat_message dispatcher — Spec §5.1, Plan §5.2."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.int.claim import ClaimKind
from primer.model.chats import Chat, ChatMessage
from primer.model.storage import OffsetPage
from primer.model.trigger import ChatMessageSubConfig, Subscription
from primer.trigger.subscribers import DispatchDeps
from primer.trigger.subscribers.chat_message import ChatMessageDispatcher


def _make_sub(chat_id: str, parallelism: str = "skip") -> Subscription:
    return Subscription(
        id="sb-1",
        trigger_id="tr-1",
        config=ChatMessageSubConfig(chat_id=chat_id),
        payload_template=None,
        parallelism=parallelism,
        enabled=True,
        created_at=datetime.now(timezone.utc),
    )


def _make_chat(chat_id: str, agent_id: str, turn_status: str = "idle") -> Chat:
    return Chat(
        id=chat_id,
        agent_id=agent_id,
        last_seq=0,
        status="active",
        turn_status=turn_status,
        created_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_dispatch_appends_user_message(
    fake_storage_provider, fake_claim_engine, fake_scheduler, seeded_agent,
):
    """Happy path — idle chat receives a fresh user_message, flips to claimable."""
    chats = fake_storage_provider.get_storage(Chat)
    await chats.create(_make_chat("cn-1", seeded_agent.id))

    deps = DispatchDeps(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        scheduler=fake_scheduler,
    )
    res = await ChatMessageDispatcher().dispatch(
        _make_sub("cn-1"),
        rendered_payload="hello",
        fire_context={
            "trigger_id": "tr-1",
            "fired_at": "2026-06-01T09:00:00+00:00",
        },
        fire_id="fire-tr-1-100",
        deps=deps,
    )
    assert res.ok is True
    assert res.skipped is False
    assert res.artefact_id is not None

    # User message landed with our text part + attribution trigger block.
    messages = fake_storage_provider.get_storage(ChatMessage)
    page = await messages.list(OffsetPage(offset=0, length=10))
    user_msgs = [m for m in page.items if m.kind == "user_message"]
    assert len(user_msgs) == 1
    payload = user_msgs[0].payload
    assert payload["parts"] == [{"type": "text", "text": "hello"}]
    assert payload["content"] == "hello"
    assert payload["trigger"] == {
        "trigger_id": "tr-1",
        "subscription_id": "sb-1",
        "fire_id": "fire-tr-1-100",
    }

    # Chat row flipped to claimable + last_seq bumped.
    rehydrated = await chats.get("cn-1")
    assert rehydrated.turn_status == "claimable"
    assert rehydrated.last_seq == 1

    # Claim engine got the high-priority pulse so a worker wakes up.
    assert (ClaimKind.CHAT, "cn-1", 10) in fake_claim_engine.upserts


@pytest.mark.asyncio
async def test_dispatch_skip_when_running(
    fake_storage_provider, fake_claim_engine, fake_scheduler, seeded_agent,
):
    """``parallelism=skip`` is a no-op when the chat is mid-turn."""
    chats = fake_storage_provider.get_storage(Chat)
    await chats.create(_make_chat("cn-1", seeded_agent.id, turn_status="running"))

    deps = DispatchDeps(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        scheduler=fake_scheduler,
    )
    res = await ChatMessageDispatcher().dispatch(
        _make_sub("cn-1", parallelism="skip"),
        rendered_payload="hello",
        fire_context={"trigger_id": "tr-1"},
        fire_id="fire-tr-1-100",
        deps=deps,
    )
    assert res.ok is True
    assert res.skipped is True
    assert res.error_code == "skipped_chat_busy"

    # No message appended, no claim pulse.
    messages = fake_storage_provider.get_storage(ChatMessage)
    page = await messages.list(OffsetPage(offset=0, length=10))
    assert page.items == []
    assert fake_claim_engine.upserts == []


@pytest.mark.asyncio
async def test_dispatch_queue_even_when_running(
    fake_storage_provider, fake_claim_engine, fake_scheduler, seeded_agent,
):
    """``parallelism=queue`` always appends — FIFO drains naturally."""
    chats = fake_storage_provider.get_storage(Chat)
    await chats.create(_make_chat("cn-1", seeded_agent.id, turn_status="running"))

    deps = DispatchDeps(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        scheduler=fake_scheduler,
    )
    res = await ChatMessageDispatcher().dispatch(
        _make_sub("cn-1", parallelism="queue"),
        rendered_payload="hello",
        fire_context={"trigger_id": "tr-1"},
        fire_id="fire-tr-1-100",
        deps=deps,
    )
    assert res.ok is True
    assert res.skipped is False
    messages = fake_storage_provider.get_storage(ChatMessage)
    page = await messages.list(OffsetPage(offset=0, length=10))
    assert len([m for m in page.items if m.kind == "user_message"]) == 1


@pytest.mark.asyncio
async def test_dispatch_chat_not_found(
    fake_storage_provider, fake_claim_engine, fake_scheduler,
):
    """Missing chat → structured ``chat_not_found`` error."""
    deps = DispatchDeps(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        scheduler=fake_scheduler,
    )
    res = await ChatMessageDispatcher().dispatch(
        _make_sub("cn-missing"),
        rendered_payload="hello",
        fire_context={"trigger_id": "tr-1"},
        fire_id="fire-tr-1-100",
        deps=deps,
    )
    assert res.ok is False
    assert res.error_code == "chat_not_found"


@pytest.mark.asyncio
async def test_dispatch_chat_ended(
    fake_storage_provider, fake_claim_engine, fake_scheduler, seeded_agent,
):
    """Ended chats refuse new turns — structured ``chat_ended`` error."""
    chats = fake_storage_provider.get_storage(Chat)
    chat = _make_chat("cn-1", seeded_agent.id)
    chat.status = "ended"
    await chats.create(chat)

    deps = DispatchDeps(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        scheduler=fake_scheduler,
    )
    res = await ChatMessageDispatcher().dispatch(
        _make_sub("cn-1"),
        rendered_payload="hello",
        fire_context={"trigger_id": "tr-1"},
        fire_id="fire-tr-1-100",
        deps=deps,
    )
    assert res.ok is False
    assert res.error_code == "chat_ended"
