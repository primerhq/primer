import json

import httpx
from typer.testing import CliRunner

from primectl.main import app

runner = CliRunner()


def test_call_custom_op_with_id(mock_session):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(200, json={"status": "ok"})

    mock_session.set_handler(handler)
    # /v1/agents/{agent_id}/status (GET) in the fixture
    result = runner.invoke(
        app, ["call", "agent", "status", "a1", "-o", "json"], obj=mock_session.session
    )
    assert result.exit_code == 0, result.output
    assert seen["method"] == "GET"
    assert seen["path"] == "/v1/agents/a1/status"
    assert json.loads(result.output)["status"] == "ok"


def test_call_unknown_action_lists_available(mock_session):
    result = runner.invoke(app, ["call", "agent", "nope"], obj=mock_session.session)
    assert result.exit_code != 0
    assert "status" in result.output  # lists available actions


def test_raw_get(mock_session):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/health"
        return httpx.Response(200, json={"status": "ok"})

    mock_session.set_handler(handler)
    result = runner.invoke(app, ["raw", "GET", "/v1/health", "-o", "json"], obj=mock_session.session)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["status"] == "ok"
