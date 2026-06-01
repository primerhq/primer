"""Trigger + Subscription model tests — Spec §3."""

from __future__ import annotations
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from primer.model.trigger import (
    Trigger, Subscription, TriggerKind, SubscriptionKind,
    DelayedTriggerConfig, ScheduledTriggerConfig,
    ChatMessageSubConfig, AgentFreshSubConfig,
    GraphFreshSubConfig, ParkedSessionSubConfig,
)
from primer.int.claim import ClaimKind


def test_claim_kind_has_trigger():
    assert ClaimKind.TRIGGER.value == "trigger"


def test_delayed_trigger_round_trips():
    t = Trigger(
        id="tr-1", slug="tr-1", name="One-off", description=None,
        config=DelayedTriggerConfig(fire_at=datetime(2026, 6, 2, 4, 0, tzinfo=timezone.utc)),
        enabled=True, next_fire_at=datetime(2026, 6, 2, 4, 0, tzinfo=timezone.utc),
        created_at=datetime.now(timezone.utc),
    )
    assert t.config.kind == "delayed"
    dumped = t.model_dump()
    rehydrated = Trigger.model_validate(dumped)
    assert rehydrated.config.kind == "delayed"


def test_scheduled_trigger_defaults():
    cfg = ScheduledTriggerConfig(cron="0 9 * * *")
    assert cfg.timezone == "UTC"
    assert cfg.catchup == "one"


def test_scheduled_trigger_catchup_validates():
    with pytest.raises(ValidationError):
        ScheduledTriggerConfig(cron="0 9 * * *", catchup="bogus")


def test_subscription_chat_message_config():
    s = Subscription(
        id="sb-1", trigger_id="tr-1",
        config=ChatMessageSubConfig(chat_id="cn-x"),
        payload_template="Hello at {{ fired_at }}",
        parallelism="skip", enabled=True,
        created_at=datetime.now(timezone.utc),
    )
    assert s.config.kind == "chat_message"
    assert s.config.chat_id == "cn-x"


def test_subscription_parallelism_validates():
    with pytest.raises(ValidationError):
        Subscription(
            id="sb-1", trigger_id="tr-1",
            config=ChatMessageSubConfig(chat_id="cn-x"),
            parallelism="bogus",
            created_at=datetime.now(timezone.utc),
        )


def test_parked_session_subscription_kind():
    s = Subscription(
        id="sb-1", trigger_id="tr-1",
        config=ParkedSessionSubConfig(
            session_id="se-1", tool_call_id="tc-1",
            parked_at=datetime.now(timezone.utc),
        ),
        created_at=datetime.now(timezone.utc),
    )
    assert s.config.kind == "parked_session"


def test_agent_fresh_subscription_kind():
    s = Subscription(
        id="sb-1", trigger_id="tr-1",
        config=AgentFreshSubConfig(workspace_id="ws-1", agent_id="ag-1"),
        created_at=datetime.now(timezone.utc),
    )
    assert s.config.kind == "agent_fresh_session"


def test_graph_fresh_subscription_kind():
    s = Subscription(
        id="sb-1", trigger_id="tr-1",
        config=GraphFreshSubConfig(workspace_id="ws-1", graph_id="gr-1"),
        created_at=datetime.now(timezone.utc),
    )
    assert s.config.kind == "graph_fresh_session"
