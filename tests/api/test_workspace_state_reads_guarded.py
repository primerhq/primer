"""A-22: the raw workspace file routes do not serve the runtime's own
``.state`` / ``.tmp`` trees to non-admins.

``.state`` holds every session's messages.jsonl (legacy ERROR rows with
tracebacks), session.json, mounts.json and the state repo; ``.tmp`` holds
truncated tool outputs. Non-admins get 403 ``forbidden_role`` on every
raw read (files/read, files/download, files/info, files/tree, files);
listings drop reserved entries; the commit diff route serves its header
but no state-repo files. Admins keep full access, for debugging. Paths
are normalised first, so ``./.state/x`` and ``a/../.state/x`` are the
same path as ``.state/x``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request

from primer.api.errors import register_error_handlers
from primer.api.deps import get_event_bus, get_scheduler, get_workspace_registry
from primer.api.routers import workspaces as ws_router
from primer.model.user import User
from primer.model.workspace import FileEntry

_NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)
_SECRET = b'{"kind":"error","payload":{"extensions":{"traceback":"Traceback ... /app/primer/x.py"}}}\n'
_USER_FILE = b"hello\n"

RESERVED_SPELLINGS = [
    ".state/sessions/s1/messages.jsonl",
    "./.state/sessions/s1/messages.jsonl",
    "a/../.state/sessions/s1/messages.jsonl",
    ".state//sessions/s1/messages.jsonl",
    "/.state/sessions/s1/messages.jsonl",
    ".tmp/s1/tool_1.txt",
    "./.tmp/s1/tool_1.txt",
    "a/../.tmp/s1/tool_1.txt",
    # The sandbox resolver turns a backslash into a separator.
    ".state\\sessions\\s1\\messages.jsonl",
    "a\\..\\.state\\sessions\\s1\\messages.jsonl",
    ".tmp\\s1\\tool_1.txt",
]


def _entry(path: str, kind: str = "file") -> FileEntry:
    return FileEntry(path=path, kind=kind, size_bytes=1, modified_at=_NOW)


class _FakeState:
    async def show_commit(self, sha: str) -> dict:
        return {
            "sha": sha, "subject": "turn 1", "body": "", "parent": "",
            "files": [
                {"path": "sessions/s1/messages.jsonl", "status": "M",
                 "patch": "+" + _SECRET.decode()},
            ],
        }


class _FakeWorkspace:
    """Reads succeed for any path (the backend does not refuse reads of
    .state: the runtime itself reads them); the guard must sit above."""

    template = SimpleNamespace(state_path=".state", tmp_path=".tmp")
    state_path = ".state"

    def __init__(self) -> None:
        self._state = _FakeState()
        self.reads: list[str] = []

    async def read_file(self, path: str) -> bytes:
        self.reads.append(path)
        return _SECRET if (".state" in path or ".tmp" in path) else _USER_FILE

    async def file_info(self, path: str) -> FileEntry:
        return _entry(path)

    async def list_files(self, path=".", *, recursive=False, max_entries=None):
        if recursive:
            return [
                _entry("src", "dir"), _entry("src/main.py"),
                _entry(".state", "dir"), _entry(".state/sessions/s1/messages.jsonl"),
                _entry(".tmp", "dir"), _entry(".tmp/s1/tool_1.txt"),
            ]
        if path in (".", ""):
            return [_entry("src", "dir"), _entry(".state", "dir"), _entry(".tmp", "dir")]
        return [_entry(f"{path.rstrip('/')}/child")]


class _Registry:
    def __init__(self) -> None:
        self.ws = _FakeWorkspace()

    async def get_workspace(self, workspace_id: str):
        return self.ws


def _app(role: str) -> tuple[FastAPI, _Registry]:
    app = FastAPI()
    register_error_handlers(app)  # PrimerError -> RFC 7807, as in the real app
    registry = _Registry()
    user = User(
        id=f"u-{role}", username=role, password_hash="!x",
        created_at=_NOW, role=role,
    )

    @app.middleware("http")
    async def _as_user(request: Request, call_next):
        request.state.user = user
        return await call_next(request)

    app.include_router(ws_router.files_router, prefix="/v1")
    app.include_router(ws_router.log_router, prefix="/v1")
    app.dependency_overrides[get_workspace_registry] = lambda: registry
    app.dependency_overrides[get_scheduler] = lambda: None
    app.dependency_overrides[get_event_bus] = lambda: None
    return app, registry


async def _get(role: str, url: str, **params):
    app, registry = _app(role)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.get(url, params=params), registry


@pytest.mark.parametrize("path", RESERVED_SPELLINGS)
@pytest.mark.parametrize("route", ["read", "download", "info"])
async def test_a_user_cannot_read_a_reserved_path(route, path):
    resp, registry = await _get("user", f"/v1/workspaces/w/files/{route}", path=path)
    assert resp.status_code == 403, resp.text
    assert "forbidden_role" in resp.text
    assert b"Traceback" not in resp.content
    assert registry.ws.reads == []


@pytest.mark.parametrize("path", [".state", "./.state/sessions", "a/../.tmp"])
@pytest.mark.parametrize("route", ["tree", ""])
async def test_a_user_cannot_list_inside_a_reserved_tree(route, path):
    url = "/v1/workspaces/w/files" + (f"/{route}" if route else "")
    resp, _ = await _get("user", url, path=path)
    assert resp.status_code == 403, resp.text
    assert "forbidden_role" in resp.text


@pytest.mark.parametrize("route", ["read", "download", "info"])
async def test_a_user_still_reads_their_own_files(route):
    resp, _ = await _get("user", f"/v1/workspaces/w/files/{route}", path="src/main.py")
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize("path", [".state/sessions/s1/messages.jsonl", "./.state/x", "a/../.tmp/s1/tool_1.txt"])
async def test_an_admin_may_read_reserved_paths_for_debugging(path):
    resp, _ = await _get("admin", "/v1/workspaces/w/files/read", path=path)
    assert resp.status_code == 200, resp.text
    assert "Traceback" in resp.json()["content"]


async def test_hidden_true_does_not_show_reserved_trees_to_a_user():
    resp, _ = await _get("user", "/v1/workspaces/w/files/tree", path=".", hidden="true")
    assert resp.status_code == 200, resp.text
    names = [i["name"] for i in resp.json()["items"]]
    assert names == ["src"]


async def test_hidden_true_still_shows_reserved_trees_to_an_admin():
    resp, _ = await _get("admin", "/v1/workspaces/w/files/tree", path=".", hidden="true")
    names = sorted(i["name"] for i in resp.json()["items"])
    assert ".state" in names


async def test_a_recursive_listing_drops_reserved_entries_for_a_user():
    resp, _ = await _get("user", "/v1/workspaces/w/files", path=".", recursive="true")
    assert resp.status_code == 200, resp.text
    paths = [i["path"] for i in resp.json()["items"]]
    assert paths == ["src", "src/main.py"]
    assert resp.json()["total"] == 2


async def test_a_recursive_listing_keeps_reserved_entries_for_an_admin():
    resp, _ = await _get("admin", "/v1/workspaces/w/files", path=".", recursive="true")
    paths = [i["path"] for i in resp.json()["items"]]
    assert ".state/sessions/s1/messages.jsonl" in paths


async def test_the_commit_diff_serves_no_state_files_to_a_user():
    resp, _ = await _get("user", "/v1/workspaces/w/commit/abcdef1")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["subject"] == "turn 1"
    assert body["files"] == []
    assert body.get("files_hidden") is True
    assert "Traceback" not in resp.text


@pytest.mark.parametrize("role", ["user", "admin"])
@pytest.mark.parametrize(
    "header", [{"If-Unmodified-Since": "Mon, 01 Jan 2001 00:00:00 GMT"}, {}],
)
async def test_a_conditional_put_into_a_reserved_tree_is_refused_before_any_stat(role, header):
    """PUT with etag / If-Unmodified-Since used to stat the path first:
    404 vs 412 told a caller whether a .state file exists. The reserved
    check (writes there are refused for everyone) now runs first."""
    app, registry = _app(role)
    stats: list[str] = []
    orig = registry.ws.file_info

    async def _counting(path):
        stats.append(path)
        return await orig(path)

    registry.ws.file_info = _counting
    params = {"path": "./.state/sessions/s1/messages.jsonl"}
    if not header:
        params["etag"] = "stale"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.put(
            "/v1/workspaces/w/files", params=params, headers=header,
            json={"content": "x", "encoding": "text"},
        )
    assert resp.status_code in (400, 403), resp.text
    assert resp.status_code != 412
    assert stats == []


def test_an_unknown_caller_is_not_an_admin():
    """Fail closed, as the tools do: no user is not an admin."""
    assert ws_router._is_admin(None) is False


async def test_the_commit_diff_serves_files_to_an_admin():
    resp, _ = await _get("admin", "/v1/workspaces/w/commit/abcdef1")
    body = resp.json()
    assert [f["path"] for f in body["files"]] == ["sessions/s1/messages.jsonl"]
    assert "files_hidden" not in body
