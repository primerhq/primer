import json
from pathlib import Path

import httpx
from typer.testing import CliRunner

from primectl.main import app

runner = CliRunner()


def test_create_from_file_posts_spec(mock_session, tmp_path: Path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "a1", "model": "gpt"})

    mock_session.set_handler(handler)
    manifest = tmp_path / "agent.yaml"
    manifest.write_text("kind: agent\nspec:\n  id: a1\n  model: gpt\n")
    result = runner.invoke(app, ["create", "-f", str(manifest)], obj=mock_session.session)
    assert result.exit_code == 0, result.output
    assert seen["method"] == "POST"
    assert seen["path"] == "/v1/agents"
    assert seen["body"] == {"id": "a1", "model": "gpt"}


def test_apply_creates_when_absent(mock_session, tmp_path: Path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(404, json={"detail": "absent"})
        return httpx.Response(201, json={"id": "a1"})

    mock_session.set_handler(handler)
    manifest = tmp_path / "a.yaml"
    manifest.write_text("kind: agent\nspec:\n  id: a1\n  model: gpt\n")
    result = runner.invoke(app, ["apply", "-f", str(manifest)], obj=mock_session.session)
    assert result.exit_code == 0, result.output
    assert ("GET", "/v1/agents/a1") in calls
    assert ("POST", "/v1/agents") in calls
    assert "created" in result.output


def test_apply_replaces_when_present(mock_session, tmp_path: Path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(200, json={"id": "a1", "model": "old"})
        return httpx.Response(200, json={"id": "a1", "model": "gpt"})

    mock_session.set_handler(handler)
    manifest = tmp_path / "a.yaml"
    manifest.write_text("kind: agent\nspec:\n  id: a1\n  model: gpt\n")
    result = runner.invoke(app, ["apply", "-f", str(manifest)], obj=mock_session.session)
    assert result.exit_code == 0, result.output
    assert ("PUT", "/v1/agents/a1") in calls
    assert "configured" in result.output


def test_apply_requires_id(mock_session, tmp_path: Path):
    manifest = tmp_path / "a.yaml"
    manifest.write_text("kind: agent\nspec:\n  model: gpt\n")
    result = runner.invoke(app, ["apply", "-f", str(manifest)], obj=mock_session.session)
    assert result.exit_code != 0
    assert "id" in result.output.lower()
