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


def test_apply_unchanged_when_spec_matches(mock_session, tmp_path: Path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            # server echoes the spec plus server-populated fields
            return httpx.Response(200, json={
                "id": "a1", "model": "gpt", "created_at": "2026-01-01T00:00:00Z",
            })
        return httpx.Response(200, json={"id": "a1"})

    mock_session.set_handler(handler)
    manifest = tmp_path / "a.yaml"
    manifest.write_text("kind: agent\nspec:\n  id: a1\n  model: gpt\n")
    result = runner.invoke(app, ["apply", "-f", str(manifest)], obj=mock_session.session)
    assert result.exit_code == 0, result.output
    assert "unchanged" in result.output
    # No PUT should have been issued.
    assert not any(m == "PUT" for m, _ in calls)


def test_apply_requires_id(mock_session, tmp_path: Path):
    manifest = tmp_path / "a.yaml"
    manifest.write_text("kind: agent\nspec:\n  model: gpt\n")
    result = runner.invoke(app, ["apply", "-f", str(manifest)], obj=mock_session.session)
    assert result.exit_code != 0
    assert "id" in result.output.lower()


def test_create_unsupported_verb_errors(mock_session):
    # 'reports' is read-only (no create_op) in the fixture.
    result = runner.invoke(
        app, ["create", "report", "--set", "title=x"], obj=mock_session.session
    )
    assert result.exit_code == 1
    assert "does not support create" in result.output


def test_edit_unsupported_verb_errors(mock_session):
    # 'llm_provider' has no update_op (no PUT) in the fixture.
    import httpx as _httpx

    def handler(request):
        return _httpx.Response(200, json={"id": "p1", "provider": "openai"})

    mock_session.set_handler(handler)
    result = runner.invoke(app, ["edit", "llm_provider", "p1"], obj=mock_session.session)
    assert result.exit_code == 1
    assert "does not support" in result.output


def test_apply_unsupported_update_errors(mock_session, tmp_path):
    # Applying to an EXISTING llm_provider needs PUT, which it does not support.
    def handler(request):
        return httpx.Response(200, json={"id": "p1", "provider": "old"})

    mock_session.set_handler(handler)
    manifest = tmp_path / "p.yaml"
    manifest.write_text("kind: llm_provider\nspec:\n  id: p1\n  provider: openai\n")
    result = runner.invoke(app, ["apply", "-f", str(manifest)], obj=mock_session.session)
    assert result.exit_code == 1
    assert "does not support" in result.output


def test_create_with_set_posts_assembled_body(mock_session):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "new-1"})

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        ["create", "agent", "--set", "description=hi", "--set", "model=gpt"],
        obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output
    assert seen["method"] == "POST"
    assert seen["path"] == "/v1/agents"
    assert seen["body"] == {"description": "hi", "model": "gpt"}
    assert "agent/new-1 created" in result.output


def test_edit_flow_puts_edited_body(mock_session, monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"id": "a1", "model": "gpt"})
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "a1", "model": "claude"})

    mock_session.set_handler(handler)
    # Simulate the user editing the model in $EDITOR (typer.edit returns the
    # edited envelope text).
    monkeypatch.setattr(
        "typer.edit",
        lambda text: "kind: agent\nspec:\n  id: a1\n  model: claude\n",
    )
    result = runner.invoke(app, ["edit", "agent", "a1"], obj=mock_session.session)
    assert result.exit_code == 0, result.output
    assert seen["method"] == "PUT"
    assert seen["path"] == "/v1/agents/a1"
    assert seen["body"] == {"id": "a1", "model": "claude"}
    assert "configured" in result.output


def test_edit_no_changes_skips_put(mock_session, monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, json={"id": "a1", "model": "gpt"})

    mock_session.set_handler(handler)
    # typer.edit returns None when the user makes no change / aborts.
    monkeypatch.setattr("typer.edit", lambda text: None)
    result = runner.invoke(app, ["edit", "agent", "a1"], obj=mock_session.session)
    assert result.exit_code == 0, result.output
    assert "no changes" in result.output
    assert "PUT" not in calls
