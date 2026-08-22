"""Platform event bus models.

Spec: ``docs/superpowers/specs/2026-08-22-event-bus-design.md``.

An :class:`Event` is one durable row in the platform event log: every
action performed on the platform (entity CRUD from the storage layer,
non-CRUD actions from the pinned emission sites) appends one. The row
is written in the same transaction as the action it describes, so the
log never claims something happened that rolled back.

An :class:`EventSubscription` is a durable consumer configuration: a
three-tier :class:`EventFilter` selecting events plus a sink saying
what to do with each match. The dispatcher tracks per-subscription
cursors in the event store (runtime state lives beside the events, not
on this config entity).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, ClassVar, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

from primer.model.common import Identifiable


class Event(BaseModel):
    """One row of the platform event log.

    ``id`` is assigned by the database (monotonic per install) and
    doubles as the subscription cursor: consuming "events after N" is
    exact, ordered, and replayable.
    """

    model_config = ConfigDict(extra="forbid")

    id: int = Field(..., description="Monotonic log position, DB-assigned.")
    event_type: str = Field(
        ...,
        description=(
            "Dotted noun.verb_past type, e.g. 'agent.created' or "
            "'session.steered'."
        ),
    )
    occurred_at: datetime
    actor: str = Field(
        default="system",
        description="Principal id that performed the action, or 'system'.",
    )
    entity_kind: str | None = None
    entity_id: str | None = None
    workspace_id: str | None = None
    session_id: str | None = None
    correlation_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


# ===========================================================================
# Filters
# ===========================================================================


class FieldMatcher(BaseModel):
    """One structural condition against the event document.

    ``path`` is dotted and resolves into the event envelope first,
    then into ``payload`` (so ``entity_kind`` and
    ``payload.collection_id`` both work). A missing path never
    matches.
    """

    model_config = ConfigDict(extra="forbid")

    path: str = Field(..., min_length=1)
    op: Literal["eq", "prefix", "regex"] = "eq"
    value: str


class EventFilter(BaseModel):
    """Three-tier filter, evaluated cheapest-first.

    1. ``event_types`` glob list (fnmatch), then ``exclude_types``.
    2. ``fields`` structural matchers, all of which must hold.
    3. ``expr`` rego expression (the approval-gate evaluator) with
       the event document as ``input``; must evaluate to a truthy
       ``allow``. Evaluation failure means no match (fail closed).
    """

    model_config = ConfigDict(extra="forbid")

    event_types: list[str] = Field(default_factory=lambda: ["*"])
    exclude_types: list[str] = Field(default_factory=list)
    fields: list[FieldMatcher] = Field(default_factory=list)
    expr: str | None = None


# ===========================================================================
# Sinks
# ===========================================================================


class ConvergeSink(BaseModel):
    """CDC: converge the event's entity page in the system collection."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["converge"] = "converge"


class LogSink(BaseModel):
    """One structured log line per matching event."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["log"] = "log"


class SessionWakeSink(BaseModel):
    """Resume a parked session, delivering the event as the payload.

    ``event_key`` is the park routing key the waiting session used
    (``evwait:{session_id}:{tool_call_id}``); the dispatcher publishes
    the matching event on the wake bus under that key and the normal
    yield-resume machinery does the rest. ``one_shot`` subscriptions
    are deleted after their first delivery.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["session_wake"] = "session_wake"
    event_key: str = Field(..., min_length=1)
    session_id: str = Field(..., min_length=1)
    one_shot: bool = True


EventSink = Annotated[
    Union[ConvergeSink, LogSink, SessionWakeSink],
    Field(discriminator="kind"),
]


class EventSubscription(Identifiable):
    """Durable consumer configuration: filter + sink.

    The dispatcher's per-subscription cursor is runtime state and
    lives in the event store, keyed by this entity's id.
    """

    _id_prefix: ClassVar[str | None] = "evsub"

    description: str = ""
    filter: EventFilter = Field(default_factory=EventFilter)
    sink: EventSink
    paused: bool = False
    managed_by: str | None = Field(
        default=None,
        description=(
            "'system' on seeded subscriptions (immutable via the API "
            "except 'paused'); None on user-defined ones."
        ),
    )


__all__ = [
    "Event",
    "FieldMatcher",
    "EventFilter",
    "ConvergeSink",
    "LogSink",
    "SessionWakeSink",
    "EventSink",
    "EventSubscription",
]
