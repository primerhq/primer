"""Tests for the ``subscribe_to_channel_event`` yielding tool (Task 17).

This generalizes ``subscribe_to_trigger`` to a channel trigger gated by an
:class:`EventMatcher`: the calling session parks, and only a matching channel
event resumes it via the existing ``parked_session`` dispatcher path. The
matcher is persisted on the one-shot :class:`Subscription` so the channel
dispatch loop can honour it before resuming.

Covers:

* A valid call yields a :class:`Yielded` keyed ``trigger:<id>`` (the same
  resume key as ``subscribe_to_trigger``) and persists a ``parked_session``
  Subscription carrying the caller's (session_id, tool_call_id) AND the
  supplied ``event_matcher``.
* A non-channel trigger is rejected with ``trigger_not_found_or_disabled``.
* A disabled trigger is rejected with the same code.
* A chat-only caller (no session id) is rejected with the same code.
* The matcher is round-tripped on the persisted Subscription.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from primer.model.storage import OffsetPage
from primer.model.trigger import (
    ChannelTriggerConfig,
    ScheduledTriggerConfig,
    Subscription,
    Trigger,
)
from primer.model.yield_ import ToolContext, Yielded
from primer.toolset.trigger import _make_subscribe_channel_handler


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _ctx() -> ToolContext:
    return ToolContext(
        tool_call_id="call-1",
        session_id="sess-1",
        workspace_id="ws-1",
    )


async def _seed_channel_trigger(
    fake_storage_provider, *, trigger_id: str = "trg-ch-1", enabled: bool = True,
) -> Trigger:
    trigger = Trigger(
        id=trigger_id,
        slug="ch-trg",
        name="Channel Trigger",
        config=ChannelTriggerConfig(provider_id="prov-1"),
        enabled=enabled,
        created_at=datetime.now(timezone.utc),
    )
    await fake_storage_provider.get_storage(Trigger).create(trigger)
    return trigger


async def _seed_scheduled_trigger(
    fake_storage_provider, *, trigger_id: str = "trg-sched-1",
) -> Trigger:
    trigger = Trigger(
        id=trigger_id,
        slug="sched-trg",
        name="Scheduled Trigger",
        config=ScheduledTriggerConfig(cron="0 2 * * *"),
        enabled=True,
        created_at=datetime.now(timezone.utc),
    )
    await fake_storage_provider.get_storage(Trigger).create(trigger)
    return trigger


# ---------------------------------------------------------------------------
# Happy path: parks + persists matcher-carrying Subscription
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parks_and_writes_channel_parked_subscription(
    fake_storage_provider,
):
    await _seed_channel_trigger(fake_storage_provider)
    handler = _make_subscribe_channel_handler(fake_storage_provider)

    result = await handler(
        {
            "trigger_id": "trg-ch-1",
            "event_matcher": {
                "event_type": "command.invoked",
                "command_name": "approve",
            },
        },
        ctx=_ctx(),
    )

    assert isinstance(result, Yielded)
    assert result.event_key == "trigger:trg-ch-1"
    assert result.tool_name == "subscribe_to_channel_event"
    sub_id = result.resume_metadata["subscription_id"]
    assert result.resume_metadata["trigger_id"] == "trg-ch-1"

    subs_storage = fake_storage_provider.get_storage(Subscription)
    sub = await subs_storage.get(sub_id)
    assert sub is not None
    assert sub.trigger_id == "trg-ch-1"
    assert sub.config.kind == "parked_session"
    assert sub.config.session_id == "sess-1"
    assert sub.config.tool_call_id == "call-1"
    assert sub.event_matcher is not None
    assert sub.event_matcher.event_type == "command.invoked"
    assert sub.event_matcher.command_name == "approve"


# ---------------------------------------------------------------------------
# Rejections
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rejects_non_channel_trigger(fake_storage_provider):
    await _seed_scheduled_trigger(fake_storage_provider)
    handler = _make_subscribe_channel_handler(fake_storage_provider)

    result = await handler(
        {
            "trigger_id": "trg-sched-1",
            "event_matcher": {"event_type": "command.invoked"},
        },
        ctx=_ctx(),
    )
    assert result.is_error
    body = json.loads(result.output)
    assert body["type"] == "trigger_not_found_or_disabled"

    subs_storage = fake_storage_provider.get_storage(Subscription)
    page = await subs_storage.list(OffsetPage(offset=0, length=10))
    assert list(page.items) == []


@pytest.mark.asyncio
async def test_rejects_disabled_trigger(fake_storage_provider):
    await _seed_channel_trigger(fake_storage_provider, enabled=False)
    handler = _make_subscribe_channel_handler(fake_storage_provider)

    result = await handler(
        {
            "trigger_id": "trg-ch-1",
            "event_matcher": {"event_type": "command.invoked"},
        },
        ctx=_ctx(),
    )
    assert result.is_error
    body = json.loads(result.output)
    assert body["type"] == "trigger_not_found_or_disabled"

    subs_storage = fake_storage_provider.get_storage(Subscription)
    page = await subs_storage.list(OffsetPage(offset=0, length=10))
    assert list(page.items) == []


@pytest.mark.asyncio
async def test_rejects_chat_only_caller(fake_storage_provider):
    await _seed_channel_trigger(fake_storage_provider)
    handler = _make_subscribe_channel_handler(fake_storage_provider)

    chat_ctx = ToolContext(
        tool_call_id="call-chat",
        session_id=None,  # chat-only invocation
        workspace_id=None,
    )
    result = await handler(
        {
            "trigger_id": "trg-ch-1",
            "event_matcher": {"event_type": "command.invoked"},
        },
        ctx=chat_ctx,
    )
    assert result.is_error
    body = json.loads(result.output)
    assert body["type"] == "trigger_not_found_or_disabled"

    subs_storage = fake_storage_provider.get_storage(Subscription)
    page = await subs_storage.list(OffsetPage(offset=0, length=10))
    assert list(page.items) == []


@pytest.mark.asyncio
async def test_matcher_is_persisted_on_subscription(fake_storage_provider):
    await _seed_channel_trigger(fake_storage_provider)
    handler = _make_subscribe_channel_handler(fake_storage_provider)

    result = await handler(
        {
            "trigger_id": "trg-ch-1",
            "event_matcher": {
                "event_type": "message.posted",
                "surface": ["slack"],
                "mentions_bot": True,
                "text_pattern": "deploy",
            },
        },
        ctx=_ctx(),
    )
    assert isinstance(result, Yielded)
    sub_id = result.resume_metadata["subscription_id"]

    sub = await fake_storage_provider.get_storage(Subscription).get(sub_id)
    assert sub is not None
    assert sub.event_matcher is not None
    assert sub.event_matcher.event_type == "message.posted"
    assert sub.event_matcher.surface == ["slack"]
    assert sub.event_matcher.mentions_bot is True
    assert sub.event_matcher.text_pattern == "deploy"
