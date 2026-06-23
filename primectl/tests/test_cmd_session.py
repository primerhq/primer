"""Session run/watch + HITL respond commands.

These prove primectl drives the session run loop purely over the existing REST
surface: create (auto_start), poll ``GET /sessions/{id}`` to terminal, render
turn-log progress, and answer ask_user / tool-approval parks via the
pending+respond endpoints. The ``run`` tests script a full
create -> running -> ask_user park -> tool_approval park -> ended sequence and
assert the CLI posts the right respond bodies in order; ``--yes`` / ``--answer``
keep them non-interactive. The ``respond`` tests cover the one-shot companions.
"""

import json

import httpx
from typer.testing import CliRunner

from primectl.main import app

runner = CliRunner()


def _ended(reason="completed"):
    return {"id": "s1", "status": "ended", "ended_reason": reason}


def test_run_no_watch_just_starts(mock_session):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "s1", "status": "running"})

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        ["session", "run", "ws1", "--agent", "a1", "-i", "do it", "--no-watch"],
        obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output
    assert seen["method"] == "POST"
    assert seen["path"] == "/v1/workspaces/ws1/sessions"
    assert seen["body"] == {
        "binding": {"kind": "agent", "agent_id": "a1"},
        "auto_start": True,
        "initial_instructions": "do it",
    }
    assert "session/s1 started" in result.output


def test_run_requires_exactly_one_binding(mock_session):
    result = runner.invoke(
        app, ["session", "run", "ws1"], obj=mock_session.session,
    )
    assert result.exit_code == 1
    assert "exactly one of --agent or --graph" in result.output


def test_run_graph_binding_with_graph_input(mock_session):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "s1", "status": "running"})

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        [
            "session", "run", "ws1", "--graph", "g1",
            "--graph-input", '{"topic": "x"}', "--no-watch",
        ],
        obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output
    assert seen["body"]["binding"] == {"kind": "graph", "graph_id": "g1"}
    assert seen["body"]["graph_input"] == {"topic": "x"}


def test_run_watch_park_respond_terminal_sequence(mock_session):
    """create -> running -> ask_user park -> approval park -> ended.

    Asserts the CLI answers the ask_user prompt and approves the tool in
    order, then stops when the session ends. ``--answer`` + ``--yes`` keep it
    non-interactive.
    """
    posts: list = []
    # Status sequence the GET /sessions/{id} poll walks through.
    statuses = iter([
        {"id": "s1", "status": "running"},
        {
            "id": "s1", "status": "waiting", "parked_status": "parked",
            "parked_state": {"yielded": {"tool_name": "ask_user"}},
        },
        {
            "id": "s1", "status": "waiting", "parked_status": "parked",
            "parked_state": {"yielded": {"tool_name": "_approval"}},
        },
        _ended(),
    ])
    current = {"row": next(statuses)}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if method == "GET" and path == "/v1/sessions/s1":
            return httpx.Response(200, json=current["row"])
        if method == "GET" and path.endswith("/turn_log"):
            return httpx.Response(200, json={"items": []})
        if method == "GET" and path.endswith("/ask_user/pending"):
            if current["row"].get("parked_state", {}).get(
                "yielded", {}
            ).get("tool_name") == "ask_user":
                return httpx.Response(
                    200,
                    json={
                        "tool_call_id": "tc-ask",
                        "prompt": "What is your name?",
                        "parked_at": "2026-06-23T00:00:00Z",
                    },
                )
            return httpx.Response(404, json={"detail": "no ask_user"})
        if method == "GET" and path.endswith("/tool_approval/pending"):
            if current["row"].get("parked_state", {}).get(
                "yielded", {}
            ).get("tool_name") == "_approval":
                return httpx.Response(
                    200,
                    json={
                        "tool_call_id": "tc-appr",
                        "tool_name": "deploy",
                        "arguments": {"env": "prod"},
                        "gate_reason": "prod deploy",
                        "parked_at": "2026-06-23T00:00:00Z",
                    },
                )
            return httpx.Response(404, json={"detail": "no approval"})
        if method == "POST" and path.endswith("/ask_user/respond"):
            posts.append(("ask_user", json.loads(request.content)))
            current["row"] = next(statuses)  # advance to approval park
            return httpx.Response(202, json={"status": "accepted"})
        if method == "POST" and path.endswith("/tool_approval/respond"):
            posts.append(("tool_approval", json.loads(request.content)))
            current["row"] = next(statuses)  # advance to ended
            return httpx.Response(202, json={"status": "accepted"})
        # POST create
        current["row"] = next(statuses)  # advance running -> ask_user park
        return httpx.Response(201, json={"id": "s1", "status": "running"})

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        [
            "session", "run", "ws1", "--agent", "a1",
            "--answer", '"Ada"', "--yes", "--poll-interval", "0",
        ],
        obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output
    assert posts[0] == ("ask_user", {"tool_call_id": "tc-ask", "response": "Ada"})
    assert posts[1] == (
        "tool_approval", {"tool_call_id": "tc-appr", "decision": "approved"},
    )
    assert "ended: completed" in result.output


def test_run_watch_create_error_surfaces(mock_session):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "no such workspace"})

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        ["session", "run", "wsX", "--agent", "a1"],
        obj=mock_session.session,
    )
    assert result.exit_code == 4  # EXIT_NOT_FOUND
    assert "not found" in result.output.lower()


def test_run_timeout(mock_session):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/sessions"):
            return httpx.Response(201, json={"id": "s1", "status": "running"})
        if request.url.path.endswith("/turn_log"):
            return httpx.Response(200, json={"items": []})
        # never ends
        return httpx.Response(200, json={"id": "s1", "status": "running"})

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        [
            "session", "run", "ws1", "--agent", "a1",
            "--poll-interval", "0", "--timeout", "0",
        ],
        obj=mock_session.session,
    )
    assert result.exit_code == 1
    assert "timed out" in result.output.lower()


def test_respond_ask_user(mock_session):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/ask_user/pending"):
            return httpx.Response(
                200,
                json={
                    "tool_call_id": "tc-ask",
                    "prompt": "color?",
                    "parked_at": "2026-06-23T00:00:00Z",
                },
            )
        seen["method"] = request.method
        seen["path"] = path
        seen["body"] = json.loads(request.content)
        return httpx.Response(202, json={"status": "accepted"})

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        ["session", "respond", "ask-user", "s1", "--response", '"blue"'],
        obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output
    assert seen["method"] == "POST"
    assert seen["path"] == "/v1/sessions/s1/ask_user/respond"
    assert seen["body"] == {"tool_call_id": "tc-ask", "response": "blue"}
    assert "ask_user answered" in result.output


def test_respond_tool_approval_with_reason(mock_session):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/tool_approval/pending"):
            return httpx.Response(
                200,
                json={
                    "tool_call_id": "tc-appr",
                    "tool_name": "rm",
                    "arguments": {},
                    "parked_at": "2026-06-23T00:00:00Z",
                },
            )
        seen["method"] = request.method
        seen["path"] = path
        seen["body"] = json.loads(request.content)
        return httpx.Response(202, json={"status": "accepted"})

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        [
            "session", "respond", "tool-approval", "s1",
            "--decision", "rejected", "--reason", "too risky",
        ],
        obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output
    assert seen["method"] == "POST"
    assert seen["path"] == "/v1/sessions/s1/tool_approval/respond"
    assert seen["body"] == {
        "tool_call_id": "tc-appr",
        "decision": "rejected",
        "reason": "too risky",
    }
    assert "tool_approval rejected" in result.output


def test_respond_tool_approval_bad_decision(mock_session):
    result = runner.invoke(
        app,
        ["session", "respond", "tool-approval", "s1", "--decision", "maybe"],
        obj=mock_session.session,
    )
    assert result.exit_code == 1
    assert "approved or rejected" in result.output


def test_respond_ask_user_not_found(mock_session):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "no pending ask_user prompt"})

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        ["session", "respond", "ask-user", "s1", "--response", '"x"'],
        obj=mock_session.session,
    )
    assert result.exit_code == 4  # EXIT_NOT_FOUND
    assert "not found" in result.output.lower()


def test_run_help_renders(mock_session):
    # A deep subcommand's --help runs the root callback, which resolves a
    # target; inject the session (as every command test does) so it
    # short-circuits config resolution and renders help instead of erroring.
    result = runner.invoke(
        app, ["session", "run", "--help"], obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output
    assert "--watch" in result.output
    assert "--agent" in result.output
    assert "--graph" in result.output
    assert "--yes" in result.output
    assert "--answer" in result.output


def test_respond_help_renders(mock_session):
    result = runner.invoke(
        app, ["session", "respond", "--help"], obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output
    assert "ask-user" in result.output
    assert "tool-approval" in result.output
