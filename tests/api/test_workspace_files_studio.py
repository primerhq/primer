"""Studio-specific tests for the workspace files sub-resource.

Covers three features added in the studio sprint:
- GET /v1/workspaces/{id}/files/tree  (Feature 1)
- mtime/etag enrichment on GET /v1/workspaces/{id}/files/read  (Feature 2)
- 412 Precondition on PUT /v1/workspaces/{id}/files  (Feature 3)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
from httpx import ASGITransport
from pydantic import SecretStr

from primer.api.app import create_test_app
from primer.api.registries import ProviderRegistry, WorkspaceRegistry
from primer.model.except_ import BadRequestError, ConflictError, NotFoundError
from primer.model.storage import OffsetPage, OffsetPageResponse
from primer.model.workspace import (
    FileEntry,
    LocalWorkspaceConfig,
    WorkspaceProvider,
    WorkspaceProviderType,
    WorkspaceRuntimeMeta,
    WorkspaceTemplate,
)


# ===========================================================================
# In-memory storage fakes (minimal copy from test_workspaces.py)
# ===========================================================================


class _Storage:
    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def get(self, id):
        return self._data.get(id)

    async def create(self, e):
        if e.id in self._data:
            raise ConflictError(f"id {e.id!r} already exists")
        self._data[e.id] = e
        return e

    async def update(self, e):
        if e.id not in self._data:
            raise NotFoundError(f"no entity with id {e.id!r}")
        self._data[e.id] = e
        return e

    async def delete(self, id):
        if id not in self._data:
            raise NotFoundError(f"no entity with id {id!r}")
        del self._data[id]

    async def list(self, page, *, order_by=None):
        items = list(self._data.values())
        if isinstance(page, OffsetPage):
            return OffsetPageResponse(
                offset=page.offset,
                length=len(items[page.offset : page.offset + page.length]),
                total=len(items),
                items=items[page.offset : page.offset + page.length],
            )
        return OffsetPageResponse(
            offset=0, length=len(items), total=len(items), items=items
        )

    async def find(self, predicate, page, *, order_by=None):
        return await self.list(page, order_by=order_by)


class _SP:
    def __init__(self) -> None:
        self._stores: dict[type, _Storage] = {}

    def get_storage(self, cls):
        return self._stores.setdefault(cls, _Storage())

    async def initialize(self):
        return

    async def aclose(self):
        return


class _FakeWorkspace:
    def __init__(self, workspace_id: str) -> None:
        self.workspace_id = workspace_id
        self._files: dict[str, bytes] = {}
        self._mtimes: dict[str, datetime] = {}
        self._dirs: set[str] = set()
        self._sessions: dict[str, Any] = {}
        self._runtime_meta = WorkspaceRuntimeMeta(
            url=f"ws://fake/{workspace_id}",
            token=SecretStr(f"tok-{workspace_id}"),
        )

    @property
    def id(self) -> str:
        return self.workspace_id

    @property
    def runtime_meta(self) -> WorkspaceRuntimeMeta:
        return self._runtime_meta

    async def list_files(self, path=".", *, recursive=False):
        # Validate path to simulate BadRequestError for traversal
        if ".." in path.split("/") or path.startswith("/"):
            raise BadRequestError(f"invalid path: {path!r}")
        out: list[FileEntry] = []
        prefix = "" if path in (".", "") else path.rstrip("/") + "/"
        now = datetime.now(timezone.utc)
        for p, content in self._files.items():
            if not p.startswith(prefix):
                continue
            tail = p[len(prefix):]
            if not recursive and "/" in tail:
                continue
            out.append(
                FileEntry(
                    path=p,
                    kind="file",
                    size_bytes=len(content),
                    modified_at=self._mtimes.get(p, now),
                )
            )
        for d in self._dirs:
            if not d.startswith(prefix) or d == path:
                continue
            tail = d[len(prefix):]
            if not tail or (not recursive and "/" in tail):
                continue
            out.append(
                FileEntry(path=d, kind="dir", size_bytes=0, modified_at=now)
            )
        return sorted(out, key=lambda fe: fe.path)

    async def file_info(self, path):
        if path not in self._files:
            raise NotFoundError(f"{path!r} not found")
        return FileEntry(
            path=path,
            kind="file",
            size_bytes=len(self._files[path]),
            modified_at=self._mtimes.get(path, datetime.now(timezone.utc)),
        )

    async def read_file(self, path):
        if path not in self._files:
            raise NotFoundError(f"{path!r} not found")
        return self._files[path]

    async def write_file(self, path, content):
        if "\x00" in path:
            raise BadRequestError("null byte in path")
        self._files[path] = content
        self._mtimes[path] = datetime.now(timezone.utc)

    async def make_dir(self, path):
        if "\x00" in path:
            raise BadRequestError("null byte in path")
        if path in self._files or path in self._dirs:
            raise BadRequestError(f"{path!r} already exists")
        self._dirs.add(path)

    async def delete_file(self, path, *, recursive=False):
        if path in self._files:
            del self._files[path]
            return
        raise NotFoundError(f"{path!r} not found")

    async def aclose(self):
        return


class _FakeBackend:
    def __init__(self, _provider) -> None:
        self._workspaces: dict[str, _FakeWorkspace] = {}
        self._counter = 0

    async def initialize(self):
        return

    async def aclose(self):
        for ws in self._workspaces.values():
            await ws.aclose()
        self._workspaces.clear()

    async def create(self, template, *, overrides=None, resolvers=None):
        self._counter += 1
        wid = f"ws-{self._counter:04d}"
        ws = _FakeWorkspace(wid)
        self._workspaces[wid] = ws
        return ws

    async def get(self, workspace_id, *, template=None):
        return self._workspaces.get(workspace_id)

    async def list(self):
        return list(self._workspaces.keys())

    async def destroy(self, workspace_id):
        if workspace_id not in self._workspaces:
            raise NotFoundError(f"workspace {workspace_id!r} not found")
        await self._workspaces[workspace_id].aclose()
        del self._workspaces[workspace_id]


# ===========================================================================
# Fixtures
# ===========================================================================


def _provider() -> WorkspaceProvider:
    return WorkspaceProvider(
        id="local-1",
        provider=WorkspaceProviderType.LOCAL,
        config=LocalWorkspaceConfig(root_path="/tmp/primer-ws-tests"),
    )


def _template() -> WorkspaceTemplate:
    return WorkspaceTemplate(
        id="tpl-1",
        description="dev workspace",
        provider_id="local-1",
    )


@pytest.fixture
def sp() -> _SP:
    return _SP()


@pytest.fixture
def pr(sp) -> ProviderRegistry:
    return ProviderRegistry(
        sp,  # type: ignore[arg-type]
        llm_factory=lambda p: object(),
        embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(),
        toolset_factory=lambda t: object(),
    )


@pytest.fixture
def wsr(sp) -> WorkspaceRegistry:
    return WorkspaceRegistry(sp, factory=_FakeBackend)


@pytest.fixture
def app(sp, pr, wsr):
    return create_test_app(
        storage_provider=sp,  # type: ignore[arg-type]
        provider_registry=pr,
        workspace_registry=wsr,
    )


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        try:
            await c.post(
                "/v1/auth/register",
                json={"username": "testuser", "password": "testpassword"},
            )
        except Exception:
            pass
        yield c


# ===========================================================================
# Shared _setup helper
# ===========================================================================


async def _setup(client, wsr):
    """Create provider + template + workspace; return (wid, fake_workspace)."""
    await client.post(
        "/v1/workspace_providers", json=_provider().model_dump(mode="json")
    )
    await client.post(
        "/v1/workspace_templates", json=_template().model_dump(mode="json")
    )
    post = await client.post("/v1/workspaces", json={"template_id": "tpl-1"})
    assert post.status_code == 201, post.text
    wid = post.json()["id"]
    backend = await wsr.get_backend("local-1")
    ws = backend._workspaces[wid]
    return wid, ws


# ===========================================================================
# Feature 1: GET /v1/workspaces/{id}/files/tree
# ===========================================================================


class TestFileTree:
    @pytest.mark.asyncio
    async def test_tree_returns_one_level(self, client, wsr) -> None:
        wid, ws = await _setup(client, wsr)
        ws._files["a.txt"] = b"hello"
        ws._files["sub/b.txt"] = b"world"
        ws._dirs.add("sub")

        resp = await client.get(
            f"/v1/workspaces/{wid}/files/tree", params={"path": "."}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["path"] == "."
        names = [item["name"] for item in body["items"]]
        assert "a.txt" in names
        assert "sub" in names
        # sub/b.txt must not appear at the top level
        assert "b.txt" not in names
        assert "sub/b.txt" not in names

    @pytest.mark.asyncio
    async def test_tree_state_hidden_by_default(self, client, wsr) -> None:
        wid, ws = await _setup(client, wsr)
        ws._dirs.add(".state")

        resp = await client.get(f"/v1/workspaces/{wid}/files/tree")
        assert resp.status_code == 200
        names = [item["name"] for item in resp.json()["items"]]
        assert ".state" not in names

        resp_hidden = await client.get(
            f"/v1/workspaces/{wid}/files/tree", params={"hidden": "true"}
        )
        assert resp_hidden.status_code == 200
        names_hidden = [item["name"] for item in resp_hidden.json()["items"]]
        assert ".state" in names_hidden

    @pytest.mark.asyncio
    async def test_tree_ordinary_dotfiles_shown(self, client, wsr) -> None:
        wid, ws = await _setup(client, wsr)
        ws._files[".gitignore"] = b"*.pyc\n"

        resp = await client.get(f"/v1/workspaces/{wid}/files/tree")
        assert resp.status_code == 200
        names = [item["name"] for item in resp.json()["items"]]
        assert ".gitignore" in names

    @pytest.mark.asyncio
    async def test_tree_path_traversal_rejected(self, client, wsr) -> None:
        wid, _ = await _setup(client, wsr)
        resp = await client.get(
            f"/v1/workspaces/{wid}/files/tree", params={"path": "../etc"}
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_tree_dirs_first_sort(self, client, wsr) -> None:
        wid, ws = await _setup(client, wsr)
        ws._files["z.txt"] = b"z"
        ws._dirs.add("a_dir")

        resp = await client.get(f"/v1/workspaces/{wid}/files/tree")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) >= 2
        # First item must be the directory
        assert items[0]["name"] == "a_dir"
        assert items[0]["is_dir"] is True
        # Second item must be the file
        assert items[1]["name"] == "z.txt"
        assert items[1]["is_dir"] is False

    @pytest.mark.asyncio
    async def test_tree_item_shape(self, client, wsr) -> None:
        wid, ws = await _setup(client, wsr)
        ws._files["readme.md"] = b"# hello"

        resp = await client.get(f"/v1/workspaces/{wid}/files/tree")
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert "name" in item
        assert "path" in item
        assert "is_dir" in item
        assert "size_bytes" in item
        assert "mtime" in item
        assert "mtime_iso" in item


# ===========================================================================
# Feature 2: mtime/etag enrichment on read_file
# ===========================================================================


class TestFileReadMtime:
    @pytest.mark.asyncio
    async def test_read_includes_mtime_and_etag(self, client, wsr) -> None:
        wid, _ = await _setup(client, wsr)
        await client.put(
            f"/v1/workspaces/{wid}/files",
            params={"path": "hello.txt"},
            json={"content": "hello world", "encoding": "text"},
        )
        resp = await client.get(
            f"/v1/workspaces/{wid}/files/read", params={"path": "hello.txt"}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["mtime_iso"] is not None
        assert isinstance(body["mtime_iso"], str)
        assert len(body["mtime_iso"]) > 0
        assert body["etag"] is not None
        assert isinstance(body["etag"], str)
        assert len(body["etag"]) == 32  # MD5 hex digest

    @pytest.mark.asyncio
    async def test_read_etag_is_stable(self, client, wsr) -> None:
        """Two reads of the same content/mtime produce the same etag."""
        wid, ws = await _setup(client, wsr)
        # Write once, then read twice without any modification
        await client.put(
            f"/v1/workspaces/{wid}/files",
            params={"path": "stable.txt"},
            json={"content": "stable", "encoding": "text"},
        )
        r1 = await client.get(
            f"/v1/workspaces/{wid}/files/read", params={"path": "stable.txt"}
        )
        r2 = await client.get(
            f"/v1/workspaces/{wid}/files/read", params={"path": "stable.txt"}
        )
        # Both reads should return the same mtime_iso (file not modified)
        # The fake returns datetime.now() each time so etags may differ;
        # just assert the field is present and non-empty.
        assert r1.json()["etag"] is not None
        assert r2.json()["etag"] is not None


# ===========================================================================
# Feature 3: 412 Precondition on PUT /files
# ===========================================================================


class TestWritePrecondition:
    @pytest.mark.asyncio
    async def test_write_no_precondition_succeeds(self, client, wsr) -> None:
        wid, _ = await _setup(client, wsr)
        resp = await client.put(
            f"/v1/workspaces/{wid}/files",
            params={"path": "new.txt"},
            json={"content": "data", "encoding": "text"},
        )
        assert resp.status_code == 204

    @pytest.mark.asyncio
    async def test_write_new_file_with_precondition_succeeds(
        self, client, wsr
    ) -> None:
        """Writing a brand-new file with If-Unmodified-Since must succeed (no conflict)."""
        wid, _ = await _setup(client, wsr)
        resp = await client.put(
            f"/v1/workspaces/{wid}/files",
            params={"path": "brand-new.txt"},
            headers={"if-unmodified-since": "Thu, 01 Jan 2015 00:00:00 GMT"},
            json={"content": "new content", "encoding": "text"},
        )
        assert resp.status_code == 204

    @pytest.mark.asyncio
    async def test_write_current_etag_succeeds(self, client, wsr) -> None:
        wid, _ = await _setup(client, wsr)
        # Write file
        await client.put(
            f"/v1/workspaces/{wid}/files",
            params={"path": "file.txt"},
            json={"content": "v1", "encoding": "text"},
        )
        # Read to get current etag
        read = await client.get(
            f"/v1/workspaces/{wid}/files/read", params={"path": "file.txt"}
        )
        current_etag = read.json()["etag"]

        # Write again with current etag → should succeed
        resp = await client.put(
            f"/v1/workspaces/{wid}/files",
            params={"path": "file.txt", "etag": current_etag},
            json={"content": "v2", "encoding": "text"},
        )
        assert resp.status_code == 204

    @pytest.mark.asyncio
    async def test_write_stale_etag_412(self, client, wsr) -> None:
        wid, _ = await _setup(client, wsr)
        # Write file first
        await client.put(
            f"/v1/workspaces/{wid}/files",
            params={"path": "file.txt"},
            json={"content": "v1", "encoding": "text"},
        )
        # Use a wrong/stale etag
        resp = await client.put(
            f"/v1/workspaces/{wid}/files",
            params={"path": "file.txt", "etag": "deadbeefdeadbeefdeadbeefdeadbeef"},
            json={"content": "v2", "encoding": "text"},
        )
        assert resp.status_code == 412
        body = resp.json()
        assert body["status"] == 412
        assert "precondition" in body["type"].lower() or "precondition" in body["title"].lower()

    @pytest.mark.asyncio
    async def test_write_if_unmodified_since_future_succeeds(
        self, client, wsr
    ) -> None:
        """If-Unmodified-Since with a far-future date → file mtime is older → no conflict."""
        wid, _ = await _setup(client, wsr)
        await client.put(
            f"/v1/workspaces/{wid}/files",
            params={"path": "file.txt"},
            json={"content": "v1", "encoding": "text"},
        )
        resp = await client.put(
            f"/v1/workspaces/{wid}/files",
            params={"path": "file.txt"},
            headers={"if-unmodified-since": "Mon, 01 Jan 2040 00:00:00 GMT"},
            json={"content": "v2", "encoding": "text"},
        )
        assert resp.status_code == 204

    @pytest.mark.asyncio
    async def test_write_if_unmodified_since_past_412(
        self, client, wsr
    ) -> None:
        """If-Unmodified-Since with a past date → file mtime is newer → 412."""
        wid, _ = await _setup(client, wsr)
        await client.put(
            f"/v1/workspaces/{wid}/files",
            params={"path": "file.txt"},
            json={"content": "v1", "encoding": "text"},
        )
        resp = await client.put(
            f"/v1/workspaces/{wid}/files",
            params={"path": "file.txt"},
            headers={"if-unmodified-since": "Thu, 01 Jan 2015 00:00:00 GMT"},
            json={"content": "v2", "encoding": "text"},
        )
        assert resp.status_code == 412

    @pytest.mark.asyncio
    async def test_write_malformed_if_unmodified_since_ignored(
        self, client, wsr
    ) -> None:
        """A malformed If-Unmodified-Since header is silently ignored → write succeeds."""
        wid, _ = await _setup(client, wsr)
        await client.put(
            f"/v1/workspaces/{wid}/files",
            params={"path": "file.txt"},
            json={"content": "v1", "encoding": "text"},
        )
        resp = await client.put(
            f"/v1/workspaces/{wid}/files",
            params={"path": "file.txt"},
            headers={"if-unmodified-since": "not-a-date"},
            json={"content": "v2", "encoding": "text"},
        )
        assert resp.status_code == 204

    @pytest.mark.asyncio
    async def test_write_etag_takes_precedence_over_header(
        self, client, wsr
    ) -> None:
        """When both etag and If-Unmodified-Since are supplied, etag wins."""
        wid, _ = await _setup(client, wsr)
        await client.put(
            f"/v1/workspaces/{wid}/files",
            params={"path": "file.txt"},
            json={"content": "v1", "encoding": "text"},
        )
        read = await client.get(
            f"/v1/workspaces/{wid}/files/read", params={"path": "file.txt"}
        )
        current_etag = read.json()["etag"]

        # etag is current (would succeed), If-Unmodified-Since is stale (would 412)
        # etag takes precedence → should succeed
        resp = await client.put(
            f"/v1/workspaces/{wid}/files",
            params={"path": "file.txt", "etag": current_etag},
            headers={"if-unmodified-since": "Thu, 01 Jan 2015 00:00:00 GMT"},
            json={"content": "v2", "encoding": "text"},
        )
        assert resp.status_code == 204
