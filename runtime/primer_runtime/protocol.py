"""Shared protocol envelope, op enum, and error codes.

This file is the runtime-side authoritative copy.
Keep in sync with ``primer/workspace/runtime/protocol.py`` on the worker side
— the two files should have identical definitions.  Changes to ops or error
codes must be mirrored in both files manually.

The runtime container does NOT depend on the ``primer`` package; having
independent copies avoids that import dependency entirely.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel


class OpName(StrEnum):
    HELLO = "hello"
    HEALTH = "health"
    READ_FILE = "read_file"
    WRITE_FILE = "write_file"
    APPEND_LINE = "append_line"
    LIST_DIR = "list_dir"
    STAT = "stat"
    DELETE = "delete"
    ARCHIVE = "archive"
    EXEC = "exec"
    WATCH_START = "watch_start"
    WATCH_CANCEL = "watch_cancel"


class ErrorCode(StrEnum):
    ENOENT = "ENOENT"
    EACCES = "EACCES"
    EISDIR = "EISDIR"
    ENOTDIR = "ENOTDIR"
    EEXIST = "EEXIST"
    ETIMEDOUT = "ETIMEDOUT"
    EUNSUPPORTED = "EUNSUPPORTED"
    EPROTOCOL = "EPROTOCOL"
    EINTERNAL = "EINTERNAL"


class Request(BaseModel):
    """Worker → Runtime: single-shot or streaming-op request."""

    req_id: int
    op: OpName
    args: dict[str, Any] | None = None

    model_config = {"frozen": True}


class Response(BaseModel):
    """Runtime → Worker: single-shot response (success or error)."""

    req_id: int
    ok: bool
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    model_config = {"frozen": True}


class Event(BaseModel):
    """Runtime → Worker: streaming event (exec stdout, watch change, etc.)."""

    req_id: int
    event: str
    data: dict[str, Any] | None = None

    model_config = {"frozen": True}


# Discriminator key written into every serialised envelope so deserialize()
# can reconstruct the correct type without ambiguity.
_TYPE_KEY = "__type__"
_TYPE_REQUEST = "req"
_TYPE_RESPONSE = "resp"
_TYPE_EVENT = "evt"


def serialize(msg: Request | Response | Event) -> str:
    """Serialise a message to a JSON text frame."""
    if isinstance(msg, Request):
        d = msg.model_dump()
        d[_TYPE_KEY] = _TYPE_REQUEST
    elif isinstance(msg, Response):
        d = msg.model_dump()
        d[_TYPE_KEY] = _TYPE_RESPONSE
    elif isinstance(msg, Event):
        d = msg.model_dump()
        d[_TYPE_KEY] = _TYPE_EVENT
    else:
        raise TypeError(f"Unknown message type: {type(msg)}")
    return json.dumps(d)


def deserialize(blob: str) -> Request | Response | Event:
    """Deserialise a JSON text frame to the appropriate message type.

    Raises ``ValueError`` if the envelope has no recognisable ``__type__``
    discriminator.
    """
    d = json.loads(blob)
    msg_type = d.pop(_TYPE_KEY)
    if msg_type == _TYPE_REQUEST:
        return Request.model_validate(d)
    if msg_type == _TYPE_RESPONSE:
        return Response.model_validate(d)
    if msg_type == _TYPE_EVENT:
        return Event.model_validate(d)
    raise ValueError(f"Unknown envelope type: {msg_type!r}")
