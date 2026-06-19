"""Path-addressed collection-document commands: doc get/put/delete/list/move.

These prove primectl stays in parity with the REST document surface added in
Task 11: the path-addressed routes on ``/v1/collections/{cid}/documents`` (the
``?path=``/``?prefix=`` query forms) and the ``/documents/move`` subroute.
"""

import json

import httpx
from typer.testing import CliRunner

from primectl.main import app

runner = CliRunner()


def test_doc_get_by_path(mock_session):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["query"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "document": {"id": "d1", "path": "notes/readme.md", "title": "Readme"},
                "content": "hello world",
            },
        )

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        ["doc", "get", "col1", "notes/readme.md", "-o", "json"],
        obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output
    assert seen["method"] == "GET"
    assert seen["path"] == "/v1/collections/col1/documents"
    assert seen["query"] == {"path": "notes/readme.md"}
    body = json.loads(result.output)
    assert body["content"] == "hello world"
    assert body["document"]["path"] == "notes/readme.md"


def test_doc_get_content_only(mock_session):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"document": {"id": "d1", "path": "a.md"}, "content": "the body"},
        )

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        ["doc", "get", "col1", "a.md", "--content"],
        obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "the body"


def test_doc_put_by_path(mock_session):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["query"] = dict(request.url.params)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"document": {"id": "d1", "path": "notes/readme.md"}}
        )

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        [
            "doc", "put", "col1", "notes/readme.md",
            "--content", "fresh content",
            "--title", "Readme",
        ],
        obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output
    assert seen["method"] == "PUT"
    assert seen["path"] == "/v1/collections/col1/documents"
    assert seen["query"] == {"path": "notes/readme.md"}
    assert seen["body"] == {"content": "fresh content", "title": "Readme"}


def test_doc_put_content_from_file(mock_session, tmp_path):
    src = tmp_path / "body.txt"
    src.write_text("file body")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"document": {"id": "d1", "path": "a.md"}})

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        ["doc", "put", "col1", "a.md", "--file", str(src)],
        obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output
    assert seen["body"]["content"] == "file body"


def test_doc_list_by_prefix(mock_session):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["query"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "documents": [
                    {"path": "notes/a.md", "document_id": "d1", "size": 12},
                    {"path": "notes/b.md", "document_id": "d2", "size": 34},
                ]
            },
        )

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        ["doc", "list", "col1", "--prefix", "notes/", "-o", "json"],
        obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output
    assert seen["method"] == "GET"
    assert seen["path"] == "/v1/collections/col1/documents"
    assert seen["query"] == {"prefix": "notes/"}
    rows = json.loads(result.output)
    assert [r["path"] for r in rows] == ["notes/a.md", "notes/b.md"]


def test_doc_delete_by_path(mock_session):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["query"] = dict(request.url.params)
        return httpx.Response(204)

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        ["doc", "delete", "col1", "notes/old.md"],
        obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output
    assert seen["method"] == "DELETE"
    assert seen["path"] == "/v1/collections/col1/documents"
    assert seen["query"] == {"path": "notes/old.md"}
    assert "deleted" in result.output


def test_doc_move(mock_session):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(204)

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        ["doc", "move", "col1", "notes/a.md", "notes/b.md"],
        obj=mock_session.session,
    )
    assert result.exit_code == 0, result.output
    assert seen["method"] == "POST"
    assert seen["path"] == "/v1/collections/col1/documents/move"
    assert seen["body"] == {"from": "notes/a.md", "to": "notes/b.md"}
    assert "moved" in result.output


def test_doc_get_not_found_surfaces_404(mock_session):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "no document at that path"})

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        ["doc", "get", "col1", "missing.md"],
        obj=mock_session.session,
    )
    assert result.exit_code == 4  # EXIT_NOT_FOUND
    assert "not found" in result.output.lower()


def test_doc_move_conflict_surfaces_409(mock_session):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "destination occupied"})

    mock_session.set_handler(handler)
    result = runner.invoke(
        app,
        ["doc", "move", "col1", "a.md", "b.md"],
        obj=mock_session.session,
    )
    assert result.exit_code == 9  # EXIT_CONFLICT
    assert "conflict" in result.output.lower()
