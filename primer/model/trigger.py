"""Trigger + Subscription models — see docs/superpowers/specs/2026-06-01-triggers-and-subscriptions-design.md §3."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationInfo, field_validator

from primer.model.common import Identifiable, reading_a_stored_row
from primer.model.event_matcher import EventMatcher
from primer.model.principal import PrincipalRef

# NOTE: ``Subscription.reply_target`` carries a ``ReplyTarget`` defined in a
# later part. Pydantic 2.13 rejects an unresolved string forward-ref on import,
# so the field is typed ``object | None`` for now; the part that defines
# ``ReplyTarget`` narrows the annotation and calls ``Subscription.model_rebuild()``.


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------


def _read_as_utc(value: datetime | None) -> datetime | None:
    """A time given or stored without a zone is read as UTC (ticket 01a16e79).

    A naive ``fire_at`` / ``next_fire_at`` used to be stored as given and armed on the claim lease, where comparing it with
    the engine's aware clock raised: on the in-memory engine one such trigger stopped every claim of every kind. Postgres's
    ``::timestamptz`` reads such a value in the session's zone, UTC on the default image. Applied on input and on the read of
    a stored row, so a legacy naive row is read as UTC too. An aware value is kept as it is.
    """
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class TriggerKind(str, Enum):
    DELAYED = "delayed"
    SCHEDULED = "scheduled"
    WEBHOOK = "webhook"
    CHANNEL = "channel"


class DelayedTriggerConfig(BaseModel):
    # extra="forbid" is what makes the API reject ``interactive`` on the
    # intrinsically non-interactive kinds (S6 section 3) with a 422 rather
    # than silently dropping it.
    model_config = ConfigDict(extra="forbid")

    kind: Literal["delayed"] = "delayed"
    fire_at: datetime  # UTC instant; a time without a zone is read as UTC

    @field_validator("fire_at")
    @classmethod
    def _fire_at_as_utc(cls, v: datetime) -> datetime:
        return _read_as_utc(v)


class ScheduledTriggerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["scheduled"] = "scheduled"
    cron: str  # validated by croniter (in trigger/cron.py)
    timezone: str = "UTC"  # IANA name (e.g. "Asia/Dubai")
    catchup: Literal["one", "all", "none"] = "one"


class WebhookTriggerConfig(BaseModel):
    """Configuration for a webhook trigger.

    ``token`` is a server-minted unguessable URL token (32 hex chars). It
    is included verbatim in the public webhook URL
    ``POST /v1/webhooks/{token}`` and must NEVER be logged in full.

    On create, ``token`` may be omitted or empty -- the service always mints
    a fresh cryptographically random token. On update (rotate), the service
    replaces whatever is stored.

    ``hmac_secret`` is optional. When set the caller must include a
    ``X-Primer-Signature`` header carrying ``HMAC-SHA256(secret, raw_body)``
    as a lowercase hex digest; requests that fail HMAC verification are
    rejected 401.
    """

    kind: Literal["webhook"] = "webhook"
    # Empty string is the caller-facing "please mint me a token" sentinel.
    # The service ALWAYS replaces this with a server-minted value before
    # persisting. Stored tokens are always exactly 32 hex chars.
    token: str = Field(default="", max_length=64)
    hmac_secret: SecretStr | None = None
    interactive: bool = Field(
        default=False,
        description=(
            "When True the POST holds its HTTP response until the fired "
            "run(s) reach a terminal state and returns their final text. "
            "Defaults off so today's 202-immediately behaviour is unchanged."
        ),
    )
    wait_timeout_seconds: int = Field(
        default=60,
        ge=1,
        le=600,
        description=(
            "How long an interactive POST holds its response before falling "
            "back to 202 + the delivery poll endpoint. Ignored when "
            "interactive is False."
        ),
    )


class ChannelTriggerConfig(BaseModel):
    kind: Literal["channel"] = "channel"
    provider_id: str
    channel_id: str | None = None
    interactive: bool = Field(
        default=True,
        description=(
            "When True the run's result posts back into the mapped platform "
            "thread after every drained turn. Defaults on: a channel "
            "conversation that never answers is not a conversation."
        ),
    )


TriggerConfig = Annotated[
    DelayedTriggerConfig
    | ScheduledTriggerConfig
    | WebhookTriggerConfig
    | ChannelTriggerConfig,
    Field(discriminator="kind"),
]


#: What a trigger slug must match. The tool and REST arguments check it too, so a bad slug is refused as an argument (``validation-error``,
#: 422) before a ``Trigger`` is built (ticket 01a16ddb, ruling 5).
SLUG_PATTERN = r"^[a-z][a-z0-9-]{1,63}$"
_SLUG_RE = re.compile(SLUG_PATTERN)


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
    owner: PrincipalRef | None = Field(
        default=None,
        description=(
            "Who last saved this trigger (server-set from the caller on "
            "create and update; a request body cannot choose it). A fired "
            "run is ranked no higher than this owner's current role. None "
            "on rows saved before owners were recorded: those fire as an "
            "ordinary user."
        ),
    )

    @field_validator("next_fire_at")
    @classmethod
    def _next_fire_at_as_utc(cls, v: datetime | None) -> datetime | None:
        return _read_as_utc(v)

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
    AGENT_FRESH_SESSION = "agent_fresh_session"
    GRAPH_FRESH_SESSION = "graph_fresh_session"
    PARKED_SESSION = "parked_session"
    SESSION_APPEND = "session_append"


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


class SessionAppendSubConfig(BaseModel):
    """Steer an EXISTING session instead of creating a fresh one.

    The rendered payload becomes a user message on ``session_id``. When the
    target already has a non-terminal turn the steer is queued as a
    ``PendingSessionMessage`` (parallelism="queue") or dropped
    (parallelism="skip"); see S6 section 3 and S1 section 4's routing rule.
    """

    kind: Literal["session_append"] = "session_append"
    session_id: str


SubscriptionConfig = Annotated[
    AgentFreshSubConfig | GraphFreshSubConfig | ParkedSessionSubConfig
    | SessionAppendSubConfig,
    Field(discriminator="kind"),
]


#: A subscription's ``parallelism``; every way in types its argument with it, on create and on update (ticket 01a16ddb, ruling 5).
Parallelism = Literal["skip", "queue"]
PARALLELISM_VALUES: tuple[str, ...] = get_args(Parallelism)


class Subscription(Identifiable):
    trigger_id: str
    config: SubscriptionConfig
    payload_template: str | None = None  # Jinja2 rendered against fire context
    parallelism: Parallelism = "skip"
    event_matcher: EventMatcher | None = None
    # ``reply_target`` carries a ``ReplyTarget`` defined in a later part. Until
    # that part lands (and calls ``Subscription.model_rebuild()``), the field is
    # typed as ``object | None`` so the model stays fully defined and importable.
    reply_target: object | None = None
    enabled: bool = True
    description: str | None = None
    last_fired_at: datetime | None = None
    last_fire_error: str | None = None
    created_at: datetime
    owner: PrincipalRef | None = Field(
        default=None,
        description=(
            "Who last saved this subscription (server-set from the caller on "
            "create and update). A fresh-session run it fires is attributed "
            "to this owner, ranked by the lower of this owner's and the "
            "trigger owner's current roles. None on rows saved before owners "
            "were recorded: those fire as an ordinary user."
        ),
    )
    reach: str | None = Field(
        default=None,
        description=(
            "The one workspace this subscription may start or feed a session "
            "in (server-set on create and update; a request body cannot "
            "choose it): the workspace of the session that last saved it, "
            "when that session was outside the default workspace (prime). "
            "None is every workspace: saved from prime, by a person or an "
            "API client, or before reach was recorded. A fire whose target is "
            "outside it is refused (target_out_of_reach) and starts no session."
        ),
    )

    @field_validator("parallelism", mode="before")
    @classmethod
    def _stored_parallelism_reads(cls, value: Any, info: ValidationInfo) -> Any:
        """A STORED row whose ``parallelism`` is neither ``skip`` nor ``queue`` reads as ``skip``, the default (ticket 01a16ddb, review of #915).

        ``update_subscription`` stored whatever string it was given until the arguments were typed (ruling 5): the ``Literal`` then refused
        such a row on every read, so the list, the get and every fire of its trigger failed. Read path tolerant, write path strict
        (:func:`~primer.model.common.reading_a_stored_row`): a request with such a value is still refused.
        """
        if reading_a_stored_row(info) and value not in PARALLELISM_VALUES:
            return "skip"
        return value


__all__ = [
    "AgentFreshSubConfig",
    "ChannelTriggerConfig",
    "DelayedTriggerConfig",
    "GraphFreshSubConfig",
    "ParkedSessionSubConfig",
    "ScheduledTriggerConfig",
    "SessionAppendSubConfig",
    "Subscription",
    "SubscriptionConfig",
    "SubscriptionKind",
    "Trigger",
    "TriggerConfig",
    "TriggerKind",
    "WebhookTriggerConfig",
]
