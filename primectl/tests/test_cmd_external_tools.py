"""external-tools command group: list, pending, respond."""

import json

import httpx
from typer.testing import CliRunner

from primectl.main import app

runner = CliRunner()


def test_list_calls_global_endpoint(mock_session):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "etool-1",
                        "tool_call_id": "tc-1",
                        "tool_name": "lookup_customer",
                        "status": "pending",
                        "session_id": "sess-1",
                        "chat_id": None,
                    }
                ],
                "total": 1,
                "offset": 0,
                "limit": 50,
            },
        )

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        ["external-tools", "list", "--status", "pending", "-o", "json"],
        obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output
    assert seen["path"] == "/v1/external_tool_calls"
    assert seen["params"] == {"status": "pending"}
    assert "tc-1" in result.output


def test_pending_requires_owner(mock_session):
    result = runner.invoke(
        app, ["external-tools", "pending"], obj=mock_session.session
    )
    assert result.exit_code != 0


def test_pending_session_endpoint(mock_session):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json={"items": []})

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        ["external-tools", "pending", "--session", "sess-1", "-o", "json"],
        obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output
    assert seen["path"] == "/v1/sessions/sess-1/external_tools/pending"


def test_respond_session_resolves_workspace_and_steers(
    mock_session, tmp_path
):
    p = tmp_path / "r.json"
    p.write_text('{"ok": true}')
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/sessions/s1":
            return httpx.Response(
                200, json={"id": "s1", "workspace_id": "w1"}
            )
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "s1", "status": "running"})

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        [
            "external-tools", "respond", "tc-1",
            "--session", "s1", "--result", f"@{p}",
        ],
        obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output
    assert seen["method"] == "POST"
    assert seen["path"] == "/v1/workspaces/w1/sessions/s1/steer"
    assert seen["body"] == {
        "tool_results": [
            {"tool_call_id": "tc-1", "result": {"ok": True}, "is_error": False}
        ]
    }


def test_respond_chat_posts_messages(mock_session):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(202, json=None)

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        [
            "external-tools", "respond", "tc-9",
            "--chat", "c1", "--result", '"done"', "--error",
        ],
        obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output
    assert seen["path"] == "/v1/chats/c1/messages"
    assert seen["body"]["tool_results"][0] == {
        "tool_call_id": "tc-9", "result": "done", "is_error": True,
    }


def test_respond_requires_exactly_one_owner(mock_session):
    result = runner.invoke(
        app,
        ["external-tools", "respond", "tc-1", "--result", "1"],
        obj=mock_session.session,
    )
    assert result.exit_code != 0
