"""Workspace file commands: workspace files get/put/ls/rm.

These prove primectl stays in parity with the workspace file REST surface: the
path-addressed routes on ``/v1/workspaces/{wid}/files`` (the ``?path=`` query
form), the ``/files/read`` subroute, and the ``encoding`` / ``recursive``
parameters. They replace the ``primectl raw`` fallback with first-class verbs.
"""

import base64
import json

import httpx
from typer.testing import CliRunner

from primectl.main import app

runner = CliRunner()


def test_files_get_by_path(mock_session):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["query"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "path": "src/app.py",
                "encoding": "text",
                "content": "print('hi')",
                "size_bytes": 11,
            },
        )

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        ["workspace", "files", "get", "ws1", "src/app.py", "-o", "json"],
        obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output
    assert seen["method"] == "GET"
    assert seen["path"] == "/v1/workspaces/ws1/files/read"
    assert seen["query"] == {"path": "src/app.py", "encoding": "text"}
    body = json.loads(result.output)
    assert body["content"] == "print('hi')"
    assert body["size_bytes"] == 11


def test_files_get_content_only(mock_session):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "path": "a.txt",
                "encoding": "text",
                "content": "the body",
                "size_bytes": 8,
            },
        )

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        ["workspace", "files", "get", "ws1", "a.txt", "--content"],
        obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "the body"


def test_files_get_base64_to_out_file(mock_session, tmp_path):
    raw = b"\x00\x01\x02binary"
    dest = tmp_path / "out.bin"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "path": "blob.bin",
                "encoding": "base64",
                "content": base64.b64encode(raw).decode("ascii"),
                "size_bytes": len(raw),
            },
        )

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        [
            "workspace", "files", "get", "ws1", "blob.bin",
            "--encoding", "base64", "--out", str(dest),
        ],
        obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output
    assert dest.read_bytes() == raw


def test_files_ls(mock_session):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["query"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "path": "a.py",
                        "kind": "file",
                        "size_bytes": 12,
                        "modified_at": "2026-06-23T00:00:00Z",
                    },
                    {
                        "path": "sub",
                        "kind": "dir",
                        "size_bytes": 0,
                        "modified_at": "2026-06-23T00:00:00Z",
                    },
                ],
                "offset": 0,
                "length": 2,
                "total": 2,
                "path": ".",
            },
        )

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        ["workspace", "files", "ls", "ws1", "-o", "json"],
        obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output
    assert seen["method"] == "GET"
    assert seen["path"] == "/v1/workspaces/ws1/files"
    assert seen["query"] == {"path": "."}
    rows = json.loads(result.output)
    assert [r["path"] for r in rows] == ["a.py", "sub"]


def test_files_ls_recursive_with_path(mock_session):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = dict(request.url.params)
        return httpx.Response(200, json={"items": []})

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        ["workspace", "files", "ls", "ws1", "src", "--recursive", "-o", "json"],
        obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output
    assert seen["query"] == {"path": "src", "recursive": "true"}


def test_files_put_by_content(mock_session):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["query"] = dict(request.url.params)
        seen["body"] = json.loads(request.content)
        return httpx.Response(204)

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        [
            "workspace", "files", "put", "ws1", "src/app.py",
            "--content", "print('hi')",
        ],
        obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output
    assert seen["method"] == "PUT"
    assert seen["path"] == "/v1/workspaces/ws1/files"
    assert seen["query"] == {"path": "src/app.py"}
    assert seen["body"] == {"content": "print('hi')", "encoding": "text"}
    assert "written" in result.output


def test_files_put_content_from_file(mock_session, tmp_path):
    src = tmp_path / "body.txt"
    src.write_text("file body")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(204)

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        ["workspace", "files", "put", "ws1", "a.txt", "--file", str(src)],
        obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output
    assert seen["body"]["content"] == "file body"
    assert seen["body"]["encoding"] == "text"


def test_files_put_base64_from_file(mock_session, tmp_path):
    raw = b"\x00\x01\x02binary"
    src = tmp_path / "blob.bin"
    src.write_bytes(raw)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(204)

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        [
            "workspace", "files", "put", "ws1", "blob.bin",
            "--file", str(src), "--encoding", "base64",
        ],
        obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output
    assert seen["body"]["encoding"] == "base64"
    assert base64.b64decode(seen["body"]["content"]) == raw


def test_files_put_requires_content_or_file(mock_session):
    result = runner.invoke(
        app,
        ["workspace", "files", "put", "ws1", "a.txt"],
        obj=mock_session.session,
    )
    assert result.exit_code == 1
    assert "needs --content or --file" in result.output


def test_files_rm_by_path(mock_session):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["query"] = dict(request.url.params)
        return httpx.Response(204)

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        ["workspace", "files", "rm", "ws1", "old.txt"],
        obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output
    assert seen["method"] == "DELETE"
    assert seen["path"] == "/v1/workspaces/ws1/files"
    assert seen["query"] == {"path": "old.txt"}
    assert "deleted" in result.output


def test_files_rm_recursive(mock_session):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = dict(request.url.params)
        return httpx.Response(204)

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        ["workspace", "files", "rm", "ws1", "build", "--recursive"],
        obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output
    assert seen["query"] == {"path": "build", "recursive": "true"}


def test_files_get_not_found_surfaces_404(mock_session):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "no such file"})

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        ["workspace", "files", "get", "ws1", "missing.txt"],
        obj=mock_session.session,
    )
    assert result.exit_code == 4  # EXIT_NOT_FOUND
    assert "not found" in result.output.lower()
