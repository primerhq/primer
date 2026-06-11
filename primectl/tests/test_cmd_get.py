import json

import httpx
from typer.testing import CliRunner

from primectl.main import app

runner = CliRunner()


def test_get_list_renders_table(mock_session):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/agents"
        return httpx.Response(200, json={
            "kind": "offset", "offset": 0, "length": 1, "total": 1,
            "items": [{"id": "a1", "model": "gpt"}],
        })

    mock_session.set_handler(handler)
    result = runner.invoke(app, ["get", "agents"], obj=mock_session.session)
    assert result.exit_code == 0, result.output
    assert "a1" in result.output


def test_get_one_by_id_emits_envelope(mock_session):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/agents/a1"
        return httpx.Response(200, json={"id": "a1", "model": "gpt"})

    mock_session.set_handler(handler)
    result = runner.invoke(app, ["get", "agent", "a1", "-o", "json"], obj=mock_session.session)
    assert result.exit_code == 0, result.output
    # Default single-object yaml/json emits the kind/spec envelope for round-trip.
    assert json.loads(result.output) == {"kind": "agent", "spec": {"id": "a1", "model": "gpt"}}


def test_get_one_raw_output_is_bare_body(mock_session):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "a1", "model": "gpt"})

    mock_session.set_handler(handler)
    result = runner.invoke(
        app, ["get", "agent", "a1", "-o", "json", "-r"], obj=mock_session.session
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["id"] == "a1"


def test_get_with_filter_posts_to_find(mock_session):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"kind": "offset", "items": [{"id": "a1"}]})

    mock_session.set_handler(handler)
    result = runner.invoke(
        app, ["get", "agents", "--filter", "model=gpt", "-o", "name"],
        obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output
    assert seen["path"] == "/v1/agents/find"
    assert seen["body"]["predicate"]["left"]["name"] == "model"
    assert result.output.strip() == "a1"


def test_get_not_found_exits_4(mock_session):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "no such agent"})

    mock_session.set_handler(handler)
    result = runner.invoke(app, ["get", "agent", "missing"], obj=mock_session.session)
    assert result.exit_code == 4
    assert "not found" in result.output.lower()


def test_describe_renders_yaml(mock_session):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "a1", "model": "gpt"})

    mock_session.set_handler(handler)
    result = runner.invoke(app, ["describe", "agent", "a1"], obj=mock_session.session)
    assert result.exit_code == 0, result.output
    assert "model: gpt" in result.output


def test_describe_honors_output_json(mock_session):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "a1", "model": "gpt"})

    mock_session.set_handler(handler)
    result = runner.invoke(
        app, ["describe", "agent", "a1", "-o", "json"], obj=mock_session.session
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["id"] == "a1"


def test_delete_calls_delete(mock_session):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(204)

    mock_session.set_handler(handler)
    result = runner.invoke(app, ["delete", "agent", "a1"], obj=mock_session.session)
    assert result.exit_code == 0, result.output
    assert seen["method"] == "DELETE"
    assert seen["path"] == "/v1/agents/a1"


def test_delete_unsupported_verb_errors(mock_session):
    # 'reports' is read-only (no delete_op) in the fixture.
    result = runner.invoke(app, ["delete", "report", "r1"], obj=mock_session.session)
    assert result.exit_code == 1
    assert "does not support delete" in result.output
