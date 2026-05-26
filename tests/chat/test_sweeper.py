"""Sweeper reclaims chats with stale heartbeats."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from matrix.chat.dispatch import sweep_chats
from matrix.model.chats import Chat, ChatMessage
from matrix.model.storage import (
    FieldRef, Op, OffsetPage, OrderBy, Predicate, Value,
)


@pytest.mark.asyncio
async def test_sweep_reclaims_stale_chat(
    fake_storage_provider, fake_provider_registry,
):
    chats = fake_storage_provider.get_storage(Chat)
    msgs = fake_storage_provider.get_storage(ChatMessage)
    now = datetime.now(timezone.utc)
    stale = now - timedelta(seconds=120)
    chat = Chat(
        id="c1", agent_id="ag", created_at=now,
        turn_status="running",
        claimed_by="dead-worker",
        claimed_at=stale, last_heartbeat_at=stale,
        last_seq=3,
    )
    await chats.create(chat)

    from matrix.bus.in_memory import InMemoryEventBus
    bus = InMemoryEventBus()
    await bus.initialize()
    from matrix.scheduler.in_memory import InMemoryScheduler
    sched = InMemoryScheduler(storage_provider=fake_storage_provider)

    reclaimed = await sweep_chats(
        storage_provider=fake_storage_provider,
        scheduler=sched,
        event_bus=bus,
        heartbeat_stale_after=timedelta(seconds=90),
    )
    assert reclaimed == 1

    row = await chats.get("c1")
    assert row.claimed_by is None
    assert row.turn_status == "claimable"
    assert row.last_seq == 4
    pred = Predicate(left=FieldRef(name="chat_id"), op=Op.EQ,
                     right=Value(value="c1"))
    page = await msgs.find(pred, OffsetPage(offset=0, length=200),
                           order_by=[OrderBy(field="seq", direction="asc")])
    assert len(page.items) == 1
    err = page.items[0]
    assert err.kind == "error"
    assert err.payload.get("code") == "worker_reclaim"
    assert err.seq == 4
    await bus.aclose()


@pytest.mark.asyncio
async def test_sweep_skips_fresh_heartbeat(
    fake_storage_provider, fake_provider_registry,
):
    chats = fake_storage_provider.get_storage(Chat)
    now = datetime.now(timezone.utc)
    chat = Chat(
        id="c2", agent_id="ag", created_at=now,
        turn_status="running",
        claimed_by="live-worker",
        last_heartbeat_at=now,
    )
    await chats.create(chat)
    from matrix.bus.in_memory import InMemoryEventBus
    bus = InMemoryEventBus()
    await bus.initialize()
    from matrix.scheduler.in_memory import InMemoryScheduler
    sched = InMemoryScheduler(storage_provider=fake_storage_provider)
    reclaimed = await sweep_chats(
        storage_provider=fake_storage_provider,
        scheduler=sched,
        event_bus=bus,
        heartbeat_stale_after=timedelta(seconds=90),
    )
    assert reclaimed == 0
    await bus.aclose()
