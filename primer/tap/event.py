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
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    YIELDED = "yielded"
    RESUMED = "resumed"
    DONE = "done"
    CANCELLED = "cancelled"
    ERROR = "error"

    # -- tap-layer extension -------------------------------------------------
    GRAPH_TRANSITION = "graph_transition"


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
