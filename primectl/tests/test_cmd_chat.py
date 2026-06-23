"""Chat convenience commands: chat say, chat switch.

Proves primectl stays in parity with the operator chat endpoints:
``POST /v1/chats/{chat_id}/messages`` body ``{content}`` -> appended
user_message ChatMessage, and ``POST /v1/chats/{chat_id}/agent`` body
``{agent_id}`` -> updated Chat.
"""

import json

import httpx
from typer.testing import CliRunner

from primectl.main import app

runner = CliRunner()


def test_chat_say(mock_session):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            202,
            json={
                "id": "chat1:1",
                "chat_id": "chat1",
                "seq": 1,
                "kind": "user_message",
                "payload": {"content": "hello there"},
            },
        )

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        ["chat", "say", "chat1", "hello there", "-o", "json"],
        obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output
    assert seen["method"] == "POST"
    assert seen["path"] == "/v1/chats/chat1/messages"
    assert seen["body"] == {"content": "hello there"}
    body = json.loads(result.output)
    assert body["kind"] == "user_message"
    assert body["seq"] == 1


def test_chat_say_empty_body_prints_ack(mock_session):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202)

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        ["chat", "say", "chat1", "ping"],
        obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output
    assert "message sent to chat chat1" in result.output


def test_chat_say_not_found_surfaces_404(mock_session):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Chat 'chatX' does not exist"})

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        ["chat", "say", "chatX", "hello"],
        obj=mock_session.session,
    )
    assert result.exit_code == 4  # EXIT_NOT_FOUND
    assert "not found" in result.output.lower()


def test_chat_say_conflict_surfaces_409(mock_session):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409, json={"detail": "Chat 'chat1' has a turn in flight"},
        )

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        ["chat", "say", "chat1", "hello"],
        obj=mock_session.session,
    )
    assert result.exit_code == 9  # EXIT_CONFLICT
    assert "conflict" in result.output.lower()


def test_chat_switch(mock_session):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"id": "chat1", "agent_id": "agent2", "status": "active"},
        )

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        ["chat", "switch", "chat1", "agent2", "-o", "json"],
        obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output
    assert seen["method"] == "POST"
    assert seen["path"] == "/v1/chats/chat1/agent"
    assert seen["body"] == {"agent_id": "agent2"}
    body = json.loads(result.output)
    assert body["agent_id"] == "agent2"


def test_chat_switch_empty_body_prints_ack(mock_session):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        ["chat", "switch", "chat1", "agent2"],
        obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output
    assert "switched to agent agent2" in result.output


def test_chat_switch_not_found_surfaces_404(mock_session):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Chat 'chatX' does not exist"})

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        ["chat", "switch", "chatX", "agent2"],
        obj=mock_session.session,
    )
    assert result.exit_code == 4  # EXIT_NOT_FOUND
    assert "not found" in result.output.lower()


def test_chat_switch_conflict_surfaces_409(mock_session):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "Chat 'chat1' has ended"})

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        ["chat", "switch", "chat1", "agent2"],
        obj=mock_session.session,
    )
    assert result.exit_code == 9  # EXIT_CONFLICT
    assert "conflict" in result.output.lower()
