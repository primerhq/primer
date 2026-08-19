"""TapEvent primitive and SessionMessageRecord -> TapEvent mapping.

A TapEvent is the normalised, wire-ready event emitted by the tap layer.
It carries every field needed by downstream consumers (SSE streams, webhooks,
analytics pipelines) without exposing internal storage details.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from primer.model.workspace_session import SessionMessageRecord


class TapEventClass(StrEnum):
    """Wire-level event class for a :class:`TapEvent`.

    Mirrors every value in :class:`~primer.model.workspace_session.SessionMessageKind`
    (1:1 string mapping) and adds :attr:`GRAPH_TRANSITION` for graph-level
    lifecycle events that have no equivalent session message kind.
    """

    # -- mirrored from SessionMessageKind (values must match exactly) --------
    USER_INPUT = "user_input"
    ASSISTANT_TOKEN = "assistant_token"
    # Model reasoning text, streamed alongside the answer by providers
    # that expose it. Display only, like its SessionMessageKind twin.
    REASONING = "reasoning"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    # Push-frame for an invoker-supplied (external) tool call, so a live
    # client sees the call as it happens and again on reconnect replay.
    EXTERNAL_TOOL_CALL = "external_tool_call"
    # Binding hand-off attribution, so a shared transcript stays
    # readable when the agent behind a session changes mid-workstream.
    AGENT_MARKER = "agent_marker"
    YIELDED = "yielded"
    RESUMED = "resumed"
    DONE = "done"
    CANCELLED = "cancelled"
    ERROR = "error"
    INVOCATION_DIVIDER = "invocation_divider"
    # Mirrors SessionMessageKind.COMPACTION_MARKER so record_to_tap_event maps
    # it 1:1 rather than crashing on the new kind (keeps the enum invariant the
    # tap tests assert). The marker is an INTERNAL history-management record,
    # not activity: the tap reader skips it (see primer/tap/reader.py) so it is
    # never surfaced on the activity rail.
    COMPACTION_MARKER = "compaction_marker"
    # ---- tap-only classes -------------------------------------------
    # Derived frames, not records. The SSE loop builds these from the
    # session row plus the log and they never advance the tap cursor,
    # so a reconnecting client re-derives the current snapshot instead
    # of replaying a historical one. They are idempotent by design:
    # receiving the same frame twice must render twice with no effect,
    # which is why they carry state rather than deltas.
    USAGE = "usage"
    PENDING_STEER = "pending_steer"
    # Structural marker for a rewind: the replay walk drops visible rows
    # past its to_seq. Nothing is deleted, so the log stays append-only.
    REWIND_MARKER = "rewind_marker"

    # -- tap-layer extension -------------------------------------------------
    GRAPH_TRANSITION = "graph_transition"
    # Mirrors SessionMessageKind.CLIENT_ACTION: the browser-facing delivery
    # frame for a notifying tool call.
    CLIENT_ACTION = "client_action"
class TapEvent(BaseModel):
    """Normalised tap event ready for wire transmission.

    The ``class`` JSON key is a reserved keyword in Python, so the field is
    named ``class_`` in Python but serialises/deserialises as ``"class"`` on
    the wire.  Use ``model_dump(by_alias=True)`` or
    ``model_dump_json(by_alias=True)`` to get the wire representation.
    ``populate_by_name=True`` lets callers construct with either ``class_``
    or ``"class"`` (via ``model_validate``).
    """

    model_config = ConfigDict(
        populate_by_name=True,
    )

    cursor: str
    seq: int
    workspace_id: str
    session_id: str
    agent_id: str | None
    graph_id: str | None
    node_id: str | None = None
    class_: TapEventClass = Field(
        ...,
        alias="class",
        serialization_alias="class",
    )
    ts: datetime
    payload: dict[str, Any]


def record_to_tap_event(
    record: SessionMessageRecord,
    *,
    workspace_id: str,
    session_id: str,
    agent_id: str | None,
    graph_id: str | None,
    cursor: str,
) -> TapEvent:
    """Map a :class:`~primer.model.workspace_session.SessionMessageRecord` to a
    :class:`TapEvent`.

    The ``kind`` field on the record maps 1:1 to ``class_`` via the shared
    string values; ``payload``, ``seq``, ``node_id``, and ``created_at`` are
    carried through unchanged (``node_id`` is ``None`` for plain agent
    sessions and set to the originating graph node for graph-run records).
    The remaining fields (``workspace_id``, ``session_id``, ``agent_id``,
    ``graph_id``, ``cursor``) are injected by the caller since they live
    outside the record itself.

    ``seq`` is copied from ``record.seq`` so the event is self-describing: the
    SSE layer reads it directly to advance the multi-session :class:`TapCursor`
    (and overwrite the per-event ``cursor`` placeholder) without parsing the
    opaque cursor string.
    """
    return TapEvent(
        cursor=cursor,
        seq=record.seq,
        workspace_id=workspace_id,
        session_id=session_id,
        agent_id=agent_id,
        graph_id=graph_id,
        node_id=record.node_id,
        class_=TapEventClass(record.kind.value),
        ts=record.created_at,
        payload=record.payload,
    )
