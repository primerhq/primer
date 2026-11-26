"""Shared protocol envelope, op enum, and error codes.

This is the authoritative definition.  The runtime-side package at
``runtime/primer_runtime/protocol.py`` re-exports everything from here so
the two sides stay in sync.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

PROTOCOL_VERSION: str = "1.8"


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
    CAPTURE_SERVICE_BUNDLE = "capture_service_bundle"
    EXEC = "exec"
    # Single-shot control request (protocol 1.4) carrying ``target_req_id``, as WATCH_CANCEL and PTY_CLOSE do: cancels the
    # in-flight EXEC with that req_id (an exec still queued on the workspace write lock never starts). A client sends it
    # only to a runtime whose hello reported protocol >= 1.4; an older runtime answers it EUNSUPPORTED.
    EXEC_CANCEL = "exec_cancel"
    WATCH_START = "watch_start"
    WATCH_CANCEL = "watch_cancel"
    EVENTS_SUBSCRIBE = "events_subscribe"
    STATE_COMMIT = "state_commit"
    STATE_READ = "state_read"
    STATE_HISTORY = "state_history"
    STATE_SHOW_COMMIT = "state_show_commit"
    # Interactive PTY ops (Studio integrated terminal). PTY_OPEN is a
    # long-lived streaming op (emits pty_open / data / exit events) keyed by
    # its req_id; PTY_STDIN / PTY_RESIZE / PTY_CLOSE are single-shot control
    # requests carrying target_req_id (mirrors WATCH_START / WATCH_CANCEL).
    PTY_OPEN = "pty_open"
    PTY_STDIN = "pty_stdin"
    PTY_RESIZE = "pty_resize"
    PTY_CLOSE = "pty_close"
    # One recursive walk in the runtime (protocol 1.8): {path, max_entries,
    # timeout_s} -> {entries: [{path, is_dir, size, mtime}], truncated}.
    # Replaces the one-list_dir-round-trip-per-directory sandbox glob walk.
    WALK = "walk"
    # One recursive regex search in the runtime (protocol 1.8):
    # {path, pattern, glob, output_mode, context, multiline,
    # case_insensitive, skip, max_files, max_file_bytes, max_matches,
    # head_limit, max_output_bytes, timeout_s} -> {lines, match_count,
    # file_count, truncated, stop_reason, stop_reasons, skipped_large}.
    # The pattern is compiled and matched with the ``regex`` library (a
    # runtime image dependency) under a per-match timeout with the GIL
    # released; a timeout stops the search with stop_reason regex_timeout.
    # Output paths are relative to the workspace root. ``max_files`` counts
    # regular files that pass ``glob`` (walked breadth-first); head_limit and
    # max_output_bytes bound the lines before they are sent; stop_reasons
    # lists every cap that fired (size_cap, file_cap, match_cap, time_cap,
    # regex_timeout, output_cap) and skipped_large counts files over
    # max_file_bytes.
    SEARCH = "search"
    # Compare-and-swap write (protocol 1.8): {path, expected_sha256,
    # content_b64, mode} -> {applied}. Under the same Tier-A write lock as
    # write_file, the current bytes are hashed and content_b64 written only
    # if the SHA-256 hex digest equals expected_sha256 (a digest, not the old
    # bytes, goes on the wire); a mismatch is applied=false, not an error.
    # Makes the sandbox ``edit`` tool's read-modify-write atomic.
    WRITE_FILE_CAS = "write_file_cas"


class ErrorCode(StrEnum):
    ENOENT = "ENOENT"
    EACCES = "EACCES"
    EISDIR = "EISDIR"
    ENOTDIR = "ENOTDIR"
    EEXIST = "EEXIST"
    EBUSY = "EBUSY"
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
