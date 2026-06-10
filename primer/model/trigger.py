"""Trigger + Subscription models — see docs/superpowers/specs/2026-06-01-triggers-and-subscriptions-design.md §3."""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator

from primer.model.common import Identifiable


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------


class TriggerKind(str, Enum):
    DELAYED = "delayed"
    SCHEDULED = "scheduled"
    # CHANNEL = "channel"   # future


class DelayedTriggerConfig(BaseModel):
    kind: Literal["delayed"] = "delayed"
    fire_at: datetime  # UTC instant


class ScheduledTriggerConfig(BaseModel):
    kind: Literal["scheduled"] = "scheduled"
    cron: str  # validated by croniter (in trigger/cron.py)
    timezone: str = "UTC"  # IANA name (e.g. "Asia/Dubai")
    catchup: Literal["one", "all", "none"] = "one"


TriggerConfig = Annotated[
    DelayedTriggerConfig | ScheduledTriggerConfig,
    Field(discriminator="kind"),
]


_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,63}$")


class Trigger(Identifiable):
    slug: str  # human-friendly id (unique)
    name: str
    description: str | None = None
    config: TriggerConfig
    enabled: bool = True
    next_fire_at: datetime | None = None  # null when disabled or terminal one-off
    last_fired_at: datetime | None = None
    last_fired_id: str | None = None  # fire_id of the last dispatched fire (dedup)
    last_fire_error: str | None = None  # JSON-encoded {code, message}
    created_at: datetime

    @field_validator("slug")
    @classmethod
    def _validate_slug(cls, v: str) -> str:
        if not _SLUG_RE.match(v):
            raise ValueError(
                "slug must match [a-z][a-z0-9-]{1,63}",
            )
        if "__" in v:
            raise ValueError("slug may not contain '__'")
        return v


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


class SubscriptionKind(str, Enum):
    CHAT_MESSAGE = "chat_message"
    AGENT_FRESH_SESSION = "agent_fresh_session"
    GRAPH_FRESH_SESSION = "graph_fresh_session"
    PARKED_SESSION = "parked_session"


class ChatMessageSubConfig(BaseModel):
    kind: Literal["chat_message"] = "chat_message"
    chat_id: str


class AgentFreshSubConfig(BaseModel):
    kind: Literal["agent_fresh_session"] = "agent_fresh_session"
    workspace_id: str
    agent_id: str


class GraphFreshSubConfig(BaseModel):
    kind: Literal["graph_fresh_session"] = "graph_fresh_session"
    workspace_id: str
    graph_id: str


class ParkedSessionSubConfig(BaseModel):
    kind: Literal["parked_session"] = "parked_session"
    session_id: str
    tool_call_id: str
    parked_at: datetime


SubscriptionConfig = Annotated[
    ChatMessageSubConfig | AgentFreshSubConfig
    | GraphFreshSubConfig | ParkedSessionSubConfig,
    Field(discriminator="kind"),
]


class Subscription(Identifiable):
    trigger_id: str
    config: SubscriptionConfig
    payload_template: str | None = None  # Jinja2 rendered against fire context
    parallelism: Literal["skip", "queue"] = "skip"
    enabled: bool = True
    description: str | None = None
    last_fired_at: datetime | None = None
    last_fire_error: str | None = None
    created_at: datetime


__all__ = [
    "AgentFreshSubConfig",
    "ChatMessageSubConfig",
    "DelayedTriggerConfig",
    "GraphFreshSubConfig",
    "ParkedSessionSubConfig",
    "ScheduledTriggerConfig",
    "Subscription",
    "SubscriptionConfig",
    "SubscriptionKind",
    "Trigger",
    "TriggerConfig",
    "TriggerKind",
]
