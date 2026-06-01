"""fire_trigger orchestrator — Spec §6."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from primer.model.trigger import (
    ChatMessageSubConfig,
    DelayedTriggerConfig,
    Subscription,
    Trigger,
)
from primer.trigger.dispatch import fire_trigger
from primer.trigger.subscribers import DispatchDeps


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_fire_skips_disabled_trigger(
    fake_storage_provider, fake_claim_engine, fake_scheduler,
):
    """A disabled trigger short-circuits without dispatching anything."""
    triggers = fake_storage_provider.get_storage(Trigger)
    t = Trigger(
        id="tr-1", slug="tr-x", name="x", description=None,
        config=DelayedTriggerConfig(fire_at=_now()),
        enabled=False,
        next_fire_at=_now(),
        created_at=_now(),
    )
    await triggers.create(t)
    deps = DispatchDeps(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        scheduler=fake_scheduler,
    )
    res = await fire_trigger(trigger_id="tr-1", scheduled_for=None, deps=deps)
    assert res.skipped is True
    assert res.fire_id is None
    assert res.results == []


@pytest.mark.asyncio
async def test_fire_dispatches_each_enabled_subscription(
    fake_storage_provider, fake_claim_engine, fake_scheduler, seeded_agent,
):
    """Happy path: enabled chat_message sub dispatches and stamps last_fired_at."""
    from primer.model.chats import Chat

    triggers = fake_storage_provider.get_storage(Trigger)
    subs = fake_storage_provider.get_storage(Subscription)
    chats = fake_storage_provider.get_storage(Chat)

    t = Trigger(
        id="tr-1", slug="tr-x", name="x", description=None,
        config=DelayedTriggerConfig(fire_at=_now()),
        enabled=True,
        next_fire_at=_now(),
        created_at=_now(),
    )
    await triggers.create(t)
    chat = Chat(
        id="cn-1", agent_id=seeded_agent.id, last_seq=0,
        status="active", turn_status="idle",
        created_at=_now(),
    )
    await chats.create(chat)
    sub = Subscription(
        id="sb-1", trigger_id="tr-1",
        config=ChatMessageSubConfig(chat_id="cn-1"),
        payload_template="hello {{ fired_at }}",
        enabled=True,
        created_at=_now(),
    )
    await subs.create(sub)

    deps = DispatchDeps(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        scheduler=fake_scheduler,
    )
    res = await fire_trigger(trigger_id="tr-1", scheduled_for=None, deps=deps)
    assert res.skipped is False
    assert res.fire_id is not None
    assert res.fire_id.startswith("fire-tr-1-")
    assert len(res.results) == 1
    assert res.results[0]["subscription_id"] == "sb-1"
    assert res.results[0]["ok"] is True

    # Trigger row's last_fired_at + last_fire_error were updated.
    updated = await triggers.get("tr-1")
    assert updated.last_fired_at is not None
    assert updated.last_fire_error is None


@pytest.mark.asyncio
async def test_fire_isolates_per_sub_failures(
    fake_storage_provider, fake_claim_engine, fake_scheduler,
):
    """A failing sub records ok=False without blocking sibling subs.

    The chat_message dispatcher returns ``ok=False,
    error_code='chat_not_found'`` when the chat row is missing; we
    assert the orchestrator surfaces that envelope and writes the
    failure into ``last_fire_error``.
    """
    triggers = fake_storage_provider.get_storage(Trigger)
    subs = fake_storage_provider.get_storage(Subscription)

    t = Trigger(
        id="tr-1", slug="tr-x", name="x", description=None,
        config=DelayedTriggerConfig(fire_at=_now()),
        enabled=True,
        next_fire_at=_now(),
        created_at=_now(),
    )
    await triggers.create(t)
    sub_bad = Subscription(
        id="sb-bad", trigger_id="tr-1",
        config=ChatMessageSubConfig(chat_id="cn-missing"),
        enabled=True,
        created_at=_now(),
    )
    await subs.create(sub_bad)

    deps = DispatchDeps(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        scheduler=fake_scheduler,
    )
    res = await fire_trigger(trigger_id="tr-1", scheduled_for=None, deps=deps)
    assert res.skipped is False
    assert len(res.results) == 1
    assert res.results[0]["ok"] is False
    assert res.results[0]["error_code"] == "chat_not_found"

    # last_fire_error is a JSON blob carrying the first failure shape.
    updated = await triggers.get("tr-1")
    assert updated.last_fire_error is not None
    blob = json.loads(updated.last_fire_error)
    assert blob["code"] == "chat_not_found"
    assert blob["subscription_id"] == "sb-bad"
