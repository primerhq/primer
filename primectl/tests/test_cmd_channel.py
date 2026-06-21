import json

import httpx
from typer.testing import CliRunner

from primectl.main import app

runner = CliRunner()


def test_binding_set_puts_reply_binding(mock_session):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content) if request.content else None
        return httpx.Response(200, json={"reply_binding": "ch-1"})

    mock_session.set_handler(handler)
    result = runner.invoke(
        app, ["channel", "binding", "set", "ws-1", "ch-1"], obj=mock_session.session
    )
    assert result.exit_code == 0, result.output
    assert seen["method"] == "PUT"
    assert seen["path"] == "/v1/workspaces/ws-1/reply_binding"
    assert seen["body"] == {"channel_id": "ch-1"}


def test_binding_clear_deletes(mock_session):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(204)

    mock_session.set_handler(handler)
    result = runner.invoke(
        app, ["channel", "binding", "clear", "ws-1"], obj=mock_session.session
    )
    assert result.exit_code == 0, result.output
    assert seen["method"] == "DELETE"
    assert seen["path"] == "/v1/workspaces/ws-1/reply_binding"


def test_binding_get_reads_workspace(mock_session):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(200, json={"id": "ws-1", "reply_binding": "ch-7"})

    mock_session.set_handler(handler)
    result = runner.invoke(
        app, ["channel", "binding", "get", "ws-1"], obj=mock_session.session
    )
    assert result.exit_code == 0, result.output
    assert seen["method"] == "GET"
    assert seen["path"] == "/v1/workspaces/ws-1"
    assert "ch-7" in result.output


def test_trigger_create_posts_channel_config(mock_session):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content) if request.content else None
        return httpx.Response(200, json={"id": "trg-9"})

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        [
            "channel",
            "trigger",
            "create",
            "--provider",
            "cp-1",
            "--channel",
            "ch-1",
            "--slug",
            "room-events",
            "--name",
            "Room events",
        ],
        obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output
    assert seen["method"] == "POST"
    assert seen["path"] == "/v1/triggers"
    assert seen["body"] == {
        "slug": "room-events",
        "name": "Room events",
        "config": {"kind": "channel", "provider_id": "cp-1", "channel_id": "ch-1"},
    }


def test_sub_create_posts_matcher_and_reply_target(mock_session):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content) if request.content else None
        return httpx.Response(200, json={"id": "sub-1"})

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        [
            "channel",
            "sub",
            "create",
            "trg-1",
            "--action",
            "agent_fresh_session",
            "--event-type",
            "command.invoked",
            "--command",
            "run",
            "--reply-target",
            "source_thread",
            "--set",
            "workspace_id=ws-1",
            "--set",
            "agent_id=ag-1",
        ],
        obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output
    assert seen["method"] == "POST"
    assert seen["path"] == "/v1/triggers/trg-1/subscriptions"
    assert seen["body"]["config"] == {
        "kind": "agent_fresh_session",
        "workspace_id": "ws-1",
        "agent_id": "ag-1",
    }
    assert seen["body"]["event_matcher"] == {
        "event_type": "command.invoked",
        "command_name": "run",
    }
    assert seen["body"]["reply_target"] == "source_thread"
