"""Tests for ``primectl tap`` — SSE stream consumer.

Uses ``httpx.MockTransport`` (via the ``mock_session`` fixture from conftest)
to feed canned SSE byte streams without hitting a real server.  Each test
invokes the Typer CLI via ``CliRunner`` and asserts on stdout, stderr, the
request URL, query params, and selector values.
"""

from __future__ import annotations

import json

import httpx
from typer.testing import CliRunner

from primectl.main import app

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sse_bytes(*frames: str) -> bytes:
    """Build a raw SSE byte payload from a list of frame strings.

    Each element of *frames* is a complete SSE frame as it would appear on
    the wire (e.g. ``"id: tok1\\ndata: {}\\n\\n"``).  They are concatenated
    and encoded as UTF-8.
    """
    return "".join(frames).encode()


_KEEPALIVE = ": keepalive\n\n"

_FRAME1 = (
    "id: cursor1\n"
    'data: {"class": "session_started", "session_id": "s1", "workspace_id": "ws1"}\n'
    "\n"
)

_FRAME2 = (
    "id: cursor2\n"
    'data: {"class": "session_ended", "session_id": "s1", "workspace_id": "ws1"}\n'
    "\n"
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_tap_prints_frames_skips_keepalive(mock_session):
    """Two data frames + a keepalive comment: only the two frames are printed."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        body = _sse_bytes(_FRAME1, _KEEPALIVE, _FRAME2)
        return httpx.Response(
            200,
            content=body,
            headers={"content-type": "text/event-stream"},
        )

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        ["tap", "ws1"],
        obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output
    lines = [l for l in result.output.splitlines() if l.strip()]
    assert len(lines) == 2
    assert json.loads(lines[0])["class"] == "session_started"
    assert json.loads(lines[1])["class"] == "session_ended"
    # No selector param when no filters given.
    assert "selector" not in seen["params"]


def test_tap_skips_pure_keepalive_frame(mock_session):
    """A stream with ONLY keepalive frames produces no stdout output."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_sse_bytes(_KEEPALIVE, _KEEPALIVE),
            headers={"content-type": "text/event-stream"},
        )

    mock_session.set_handler(handler)
    result = runner.invoke(app, ["tap", "ws1"], obj=mock_session.session)
    assert result.exit_code == 0, result.output
    assert result.output.strip() == ""


def test_tap_event_class_flag_builds_selector(mock_session):
    """--event-class builds events predicate class IN [...] in the selector."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            content=_sse_bytes(_FRAME1),
            headers={"content-type": "text/event-stream"},
        )

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        ["tap", "ws1", "--event-class", "session_started", "--event-class", "session_ended"],
        obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output

    assert "selector" in seen["params"]
    sel = json.loads(seen["params"]["selector"])
    # events predicate: class IN [session_started, session_ended]
    assert "events" in sel
    events_pred = sel["events"]
    assert events_pred["left"]["name"] == "class"
    assert events_pred["op"].upper() == "IN"
    assert set(events_pred["right"]["value"]) == {"session_started", "session_ended"}


def test_tap_session_flag_builds_selector(mock_session):
    """--session builds sessions predicate id IN [...] in the selector."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            content=_sse_bytes(_FRAME1),
            headers={"content-type": "text/event-stream"},
        )

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        ["tap", "ws1", "--session", "s1", "--session", "s2"],
        obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output

    assert "selector" in seen["params"]
    sel = json.loads(seen["params"]["selector"])
    assert "sessions" in sel
    sessions_pred = sel["sessions"]
    assert sessions_pred["left"]["name"] == "id"
    assert sessions_pred["op"].upper() == "IN"
    assert set(sessions_pred["right"]["value"]) == {"s1", "s2"}


def test_tap_selector_json_wins_over_flags(mock_session):
    """--selector-json takes precedence over --event-class / --session."""
    seen: dict = {}
    raw_selector = '{"events": null, "sessions": null}'

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            content=_sse_bytes(_FRAME1),
            headers={"content-type": "text/event-stream"},
        )

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        [
            "tap", "ws1",
            "--event-class", "session_started",
            "--selector-json", raw_selector,
        ],
        obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output
    # The raw selector JSON should be passed as-is (re-serialised but same content).
    assert "selector" in seen["params"]
    # The raw_selector itself is valid; it should be passed through.
    assert seen["params"]["selector"] == raw_selector


def test_tap_cursor_sent_as_query_param(mock_session):
    """--cursor passes the token as ?cursor= query parameter."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            content=_sse_bytes(_FRAME1),
            headers={"content-type": "text/event-stream"},
        )

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        ["tap", "ws1", "--cursor", "abc123"],
        obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output
    assert seen["params"].get("cursor") == "abc123"


def test_tap_prints_last_cursor_on_stderr(mock_session):
    """After streaming, the last cursor is printed to stderr for resume."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_sse_bytes(_FRAME1, _FRAME2),
            headers={"content-type": "text/event-stream"},
        )

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        ["tap", "ws1"],
        obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output
    assert "cursor2" in result.stderr


def test_tap_pretty_prints_json(mock_session):
    """--pretty produces indented JSON output."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_sse_bytes(_FRAME1),
            headers={"content-type": "text/event-stream"},
        )

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        ["tap", "ws1", "--pretty"],
        obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output
    # Pretty output spans multiple lines for a single event.
    lines = result.output.splitlines()
    assert len(lines) > 1
    # It should still be valid JSON when joined.
    combined = "\n".join(lines)
    parsed = json.loads(combined)
    assert parsed["class"] == "session_started"


def test_tap_http_error_exits_nonzero(mock_session):
    """A non-2xx response exits with code 1 and prints the error."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "auth_required"})

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        ["tap", "ws1"],
        obj=mock_session.session,
    )
    assert result.exit_code == 1


def test_tap_request_path_is_correct(mock_session):
    """The request is made to /v1/workspaces/{wid}/tap."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(
            200,
            content=_sse_bytes(_FRAME1),
            headers={"content-type": "text/event-stream"},
        )

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        ["tap", "my-workspace"],
        obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output
    assert seen["path"] == "/v1/workspaces/my-workspace/tap"


def test_tap_invalid_selector_json_exits(mock_session):
    """An invalid --selector-json value exits with code 1."""
    result = runner.invoke(
        app,
        ["tap", "ws1", "--selector-json", "not-valid-json{{{"],
        obj=mock_session.session,
    )
    assert result.exit_code == 1
