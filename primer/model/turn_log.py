"""TurnLogEvent discriminated union + TurnLogRecord storage entity.

See ``docs/superpowers/specs/2026-06-05-per-session-turn-log-design.md``
for the full design. Six core kinds (started, completed, failed,
yielded, resumed, cancelled) carry the same shape across agent
sessions and graph nodes; two graph-only kinds (superstep_started,
superstep_ended) land in the graph-level file.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter

from primer.model.common import Identifiable
from primer.model.problem_details import ProblemDetails


class TurnLogKind(str, Enum):
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    YIELDED = "yielded"
    RESUMED = "resumed"
    CANCELLED = "cancelled"
    SUPERSTEP_STARTED = "superstep_started"
    SUPERSTEP_ENDED = "superstep_ended"


class _TurnLogBase(BaseModel):
    """Common fields on every event."""

    seq: int = Field(
        ...,
        description="Per-file monotonic seq; writer-controlled.",
    )
    ts: datetime = Field(..., description="UTC event timestamp.")

    node_id: str | None = Field(default=None)
    iteration: int | None = Field(default=None)
    superstep_id: str | None = Field(default=None)

    turn_no: int | None = Field(default=None)


class TurnLogStarted(_TurnLogBase):
    kind: Literal[TurnLogKind.STARTED] = TurnLogKind.STARTED
    model: str | None = None
    input_message_count: int = 0


class TurnLogCompleted(_TurnLogBase):
    kind: Literal[TurnLogKind.COMPLETED] = TurnLogKind.COMPLETED
    duration_ms: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    finish_reason: str | None = None


class TurnLogFailed(_TurnLogBase):
    kind: Literal[TurnLogKind.FAILED] = TurnLogKind.FAILED
    duration_ms: int = 0
    error: ProblemDetails


class TurnLogYielded(_TurnLogBase):
    kind: Literal[TurnLogKind.YIELDED] = TurnLogKind.YIELDED
    yield_kind: Literal["ask_user", "subscribe_to_trigger", "approval"]
    event_key: str


class TurnLogResumed(_TurnLogBase):
    kind: Literal[TurnLogKind.RESUMED] = TurnLogKind.RESUMED
    wait_ms: int = 0
    resume_kind: Literal["event_fired", "operator_resume", "timeout"]


class TurnLogCancelled(_TurnLogBase):
    kind: Literal[TurnLogKind.CANCELLED] = TurnLogKind.CANCELLED
    reason: str | None = None


class TurnLogSuperstepStarted(_TurnLogBase):
    kind: Literal[TurnLogKind.SUPERSTEP_STARTED] = TurnLogKind.SUPERSTEP_STARTED
    ready_node_ids: list[str] = Field(default_factory=list)


class TurnLogSuperstepEnded(_TurnLogBase):
    kind: Literal[TurnLogKind.SUPERSTEP_ENDED] = TurnLogKind.SUPERSTEP_ENDED
    completed_node_ids: list[str] = Field(default_factory=list)
    failed_node_ids: list[str] = Field(default_factory=list)
    duration_ms: int = 0


TurnLogEvent = Annotated[
    Union[
        TurnLogStarted,
        TurnLogCompleted,
        TurnLogFailed,
        TurnLogYielded,
        TurnLogResumed,
        TurnLogCancelled,
        TurnLogSuperstepStarted,
        TurnLogSuperstepEnded,
    ],
    Field(discriminator="kind"),
]


_EVENT_ADAPTER: TypeAdapter[TurnLogEvent] = TypeAdapter(TurnLogEvent)


def parse_turn_log_event(data: dict[str, Any]) -> TurnLogEvent:
    """Parse a dict into the right TurnLogEvent subclass via discriminator."""
    return _EVENT_ADAPTER.validate_python(data)


class TurnLogRecord(Identifiable):
    """Storage entity for the StorageGraphExecutor variant.

    Mirrors the JSONL file shape but carries the parent's id (``run_id``)
    + optional ``node_id`` as separate columns so the query layer can
    index without parsing the payload blob.
    """

    run_id: str = Field(..., description="GraphThread.id or Session.id")
    node_id: str | None = Field(default=None)
    seq: int = Field(..., description="Per (run_id, node_id) monotonic.")
    kind: TurnLogKind
    iteration: int | None = None
    superstep_id: str | None = None
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Serialised TurnLogEvent excluding the common base fields. "
            "Parsed back into the right TurnLogEvent subclass on read."
        ),
    )
    created_at: datetime


__all__ = [
    "TurnLogKind",
    "TurnLogStarted",
    "TurnLogCompleted",
    "TurnLogFailed",
    "TurnLogYielded",
    "TurnLogResumed",
    "TurnLogCancelled",
    "TurnLogSuperstepStarted",
    "TurnLogSuperstepEnded",
    "TurnLogEvent",
    "TurnLogRecord",
    "parse_turn_log_event",
]
