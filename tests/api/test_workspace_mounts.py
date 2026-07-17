"""Route tests for the Collection<->Workspace mounts sub-resource (Task 5).

Combines the two existing fixture patterns rather than re-inventing either:

* workspace creation — mirrors ``tests/api/test_workspace_files_studio.py``
  (provider + template + POST /v1/workspaces, backed by an in-memory
  ``_FakeBackend``/``_FakeWorkspace`` pair so file ops don't touch a real
  filesystem or subprocess).
* collection + document creation — mirrors
  ``tests/api/test_knowledge_documents_by_path.py`` (PUT
  ``/v1/collections/{id}/documents?path=...``), except it uses the shared
  ``_FakeStorageProvider`` (which already implements ``transaction()`` +
  a real in-memory content store) instead of a real sqlite provider, so a
  single storage provider backs both the collection/document routes AND the
  workspace registry in one app.
"""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport

from primer.api.app import create_test_app
from primer.api.registries import ProviderRegistry, WorkspaceRegistry
from primer.model.collection import Collection, CollectionEmbedder
from primer.model.except_ import NotFoundError
from tests.api.test_workspace_files_studio import _FakeBackend, _FakeWorkspace, _provider, _template
from tests.conftest import _FakeStorageProvider


class _MountFakeWorkspace(_FakeWorkspace):
    """``_FakeWorkspace`` + two fidelity fixes the mounts path needs.

    The shared fake (test_workspace_files_studio.py) is intentionally
    minimal; the mounts router exercises two contracts it doesn't model,
    so extend it here rather than mutate the shared fixture other test
    modules depend on:

    * ``delete_file(dest, recursive=True)`` — detach removes a whole
      mount-root directory; the shared fake only ever deleted a single
      file key.
    * ``list_files(missing_path)`` — the REAL backends (local/sandbox) raise
      primer's :class:`NotFoundError` for a path that does not exist, NOT an
      empty list. The dest-exists check and the post-detach "gone" assertions
      only test the real not-found contract if the fake matches it.
    """

    async def list_files(self, path=".", *, recursive=False):
        # Preserve the parent's traversal validation (it raises
        # BadRequestError) by delegating invalid paths straight through.
        if ".." in path.split("/") or path.startswith("/"):
            return await super().list_files(path, recursive=recursive)
        # Root always exists; any other path must exist as a dir or have at
        # least one entry under it, else it's a not-found (real-backend parity).
        if path not in (".", ""):
            prefix = path.rstrip("/") + "/"
            exists = (
                path in self._dirs
                or any(p == path or p.startswith(prefix) for p in self._files)
                or any(d == path or d.startswith(prefix) for d in self._dirs)
            )
            if not exists:
                raise NotFoundError(f"{path!r} not found")
        return await super().list_files(path, recursive=recursive)

    async def delete_file(self, path, *, recursive=False):
        if path in self._files:
            del self._files[path]
            self._mtimes.pop(path, None)
            return
        if recursive:
            prefix = path.rstrip("/") + "/"
            removed = False
            for p in list(self._files):
                if p == path or p.startswith(prefix):
                    del self._files[p]
                    self._mtimes.pop(p, None)
                    removed = True
            for d in list(self._dirs):
                if d == path or d.startswith(prefix):
                    self._dirs.discard(d)
                    removed = True
            if removed:
                return
        raise NotFoundError(f"{path!r} not found")


class _MountFakeBackend(_FakeBackend):
    async def create(self, template, *, overrides=None, resolvers=None):
        self._counter += 1
        wid = f"ws-{self._counter:04d}"
        ws = _MountFakeWorkspace(wid)
        self._workspaces[wid] = ws
        return ws


_SSP_BODY = {
    "id": "ssp-test",
    "provider": "pgvector",
    "config": {
        "hostname": "localhost",
        "port": 5432,
        "database": "primer",
        "username": "primer",
        "password": "primer",
        "db_schema": "public",
    },
}


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def sp() -> _FakeStorageProvider:
    return _FakeStorageProvider()


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
    return WorkspaceRegistry(sp, factory=_MountFakeBackend)  # type: ignore[arg-type]


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
# Shared setup helpers
# ===========================================================================


async def _setup_workspace(client, wsr) -> str:
    """Create provider + template + a running workspace; return its id."""
    await client.post(
        "/v1/workspace_providers", json=_provider().model_dump(mode="json")
    )
    await client.post(
        "/v1/workspace_templates", json=_template().model_dump(mode="json")
    )
    post = await client.post("/v1/workspaces", json={"template_id": "tpl-1"})
    assert post.status_code == 201, post.text
    return post.json()["id"]


async def _make_collection(client, collection_id: str) -> str:
    """Create the search provider (idempotent) + an empty collection."""
    await client.post("/v1/ssp", json=_SSP_BODY)
    body = Collection(
        id=collection_id,
        description="test collection",
        embedder=CollectionEmbedder(provider_id="hf-1", model="all-MiniLM-L6-v2"),
        search_provider_id="ssp-test",
    ).model_dump(mode="json")
    created = await client.post("/v1/collections", json=body)
    assert created.status_code == 201, created.text
    return collection_id


async def _setup_collection(client, collection_id: str = "kb-1") -> str:
    """Create a search provider + collection with two documents."""
    await _make_collection(client, collection_id)
    for path, content in (("a.md", "alpha"), ("b.md", "beta")):
        r = await client.put(
            f"/v1/collections/{collection_id}/documents",
            params={"path": path},
            json={"content": content},
        )
        assert r.status_code in (200, 201), r.text
    return collection_id


# ===========================================================================
# POST /v1/workspaces/{id}/mounts — import
# ===========================================================================


@pytest.mark.asyncio
async def test_import_mounts_collection(client, wsr) -> None:
    wid = await _setup_workspace(client, wsr)
    coll_id = await _setup_collection(client)

    r = await client.post(
        f"/v1/workspaces/{wid}/mounts",
        json={"collection_id": coll_id, "dest": "docs-mount"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["collection_id"] == coll_id
    assert body["dest"] == "docs-mount"
    assert body["mount_id"]
    assert {b["path"] for b in body["base"]} == {"a.md", "b.md"}

    # The imported files land under dest.
    tree = await client.get(
        f"/v1/workspaces/{wid}/files/tree", params={"path": "docs-mount"}
    )
    assert tree.status_code == 200, tree.text
    names = {item["name"] for item in tree.json()["items"]}
    assert names == {"a.md", "b.md"}

    # The root tree marks the dest dir as a collection-origin mount root.
    root_tree = await client.get(f"/v1/workspaces/{wid}/files/tree")
    assert root_tree.status_code == 200, root_tree.text
    dest_items = [i for i in root_tree.json()["items"] if i["name"] == "docs-mount"]
    assert len(dest_items) == 1
    assert dest_items[0]["is_dir"] is True
    assert dest_items[0]["origin"] == "collection"


@pytest.mark.asyncio
async def test_import_defaults_name_and_dest_to_collection_id(client, wsr) -> None:
    # Regression: a Collection has no name field. The manifest collection_name
    # and the default dest must be the id, NEVER the (long) description
    # ("test collection") -- that conflation polluted the Studio UI + manifest.
    wid = await _setup_workspace(client, wsr)
    coll_id = await _setup_collection(client)  # description="test collection"
    r = await client.post(
        f"/v1/workspaces/{wid}/mounts",
        json={"collection_id": coll_id},  # no dest -> should default to the id
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["collection_name"] == coll_id
    assert body["collection_name"] != "test collection"
    assert body["dest"] == coll_id


@pytest.mark.asyncio
async def test_import_missing_collection_404(client, wsr) -> None:
    wid = await _setup_workspace(client, wsr)
    r = await client.post(
        f"/v1/workspaces/{wid}/mounts",
        json={"collection_id": "does-not-exist"},
    )
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_import_twice_conflicts(client, wsr) -> None:
    wid = await _setup_workspace(client, wsr)
    coll_id = await _setup_collection(client)

    first = await client.post(
        f"/v1/workspaces/{wid}/mounts",
        json={"collection_id": coll_id, "dest": "docs-mount"},
    )
    assert first.status_code == 201, first.text

    second = await client.post(
        f"/v1/workspaces/{wid}/mounts",
        json={"collection_id": coll_id, "dest": "docs-mount-2"},
    )
    assert second.status_code == 409, second.text


@pytest.mark.asyncio
async def test_import_dest_exists_conflicts(client, wsr) -> None:
    wid = await _setup_workspace(client, wsr)
    coll_id = await _setup_collection(client)

    # Pre-occupy the dest with a real file before attempting the mount.
    pre = await client.put(
        f"/v1/workspaces/{wid}/files",
        params={"path": "taken/blocker.txt"},
        json={"content": "blocker", "encoding": "text"},
    )
    assert pre.status_code == 204, pre.text

    r = await client.post(
        f"/v1/workspaces/{wid}/mounts",
        json={"collection_id": coll_id, "dest": "taken"},
    )
    assert r.status_code == 409, r.text


# ===========================================================================
# GET /v1/workspaces/{id}/mounts — list
# ===========================================================================


@pytest.mark.asyncio
async def test_list_mounts(client, wsr) -> None:
    wid = await _setup_workspace(client, wsr)
    coll_id = await _setup_collection(client)

    empty = await client.get(f"/v1/workspaces/{wid}/mounts")
    assert empty.status_code == 200, empty.text
    assert empty.json()["mounts"] == []

    created = (
        await client.post(
            f"/v1/workspaces/{wid}/mounts",
            json={"collection_id": coll_id, "dest": "docs-mount"},
        )
    ).json()

    listed = await client.get(f"/v1/workspaces/{wid}/mounts")
    assert listed.status_code == 200, listed.text
    mounts = listed.json()["mounts"]
    assert len(mounts) == 1
    assert mounts[0]["mount_id"] == created["mount_id"]
    assert mounts[0]["collection_id"] == coll_id
    assert mounts[0]["dest"] == "docs-mount"


@pytest.mark.asyncio
async def test_list_mounts_dirty_flag(client, wsr) -> None:
    """`dirty` (Task 11) is False right after import, then True once a file
    under the mount's dest diverges from the base snapshot — mirrors the
    modified-detection `test_detach_modified_requires_force` already
    exercises, but observed through GET /mounts instead of the 409 path."""
    wid = await _setup_workspace(client, wsr)
    coll_id = await _setup_collection(client)
    m = (
        await client.post(
            f"/v1/workspaces/{wid}/mounts",
            json={"collection_id": coll_id, "dest": "docs-mount"},
        )
    ).json()

    clean = await client.get(f"/v1/workspaces/{wid}/mounts")
    assert clean.status_code == 200, clean.text
    assert clean.json()["mounts"][0]["dirty"] is False

    dest = m["dest"]
    doc_path = m["base"][0]["path"]
    edit = await client.put(
        f"/v1/workspaces/{wid}/files",
        params={"path": f"{dest}/{doc_path}"},
        json={"content": "CHANGED", "encoding": "text"},
    )
    assert edit.status_code == 204, edit.text

    dirty = await client.get(f"/v1/workspaces/{wid}/mounts")
    assert dirty.status_code == 200, dirty.text
    assert dirty.json()["mounts"][0]["dirty"] is True


# ===========================================================================
# DELETE /v1/workspaces/{id}/mounts/{mount_id} — detach
# ===========================================================================


@pytest.mark.asyncio
async def test_detach_clean(client, wsr) -> None:
    wid = await _setup_workspace(client, wsr)
    coll_id = await _setup_collection(client)
    m = (
        await client.post(
            f"/v1/workspaces/{wid}/mounts",
            json={"collection_id": coll_id, "dest": "docs-mount"},
        )
    ).json()

    # Sanity: before detach the dest dir shows on the root tree.
    before = await client.get(f"/v1/workspaces/{wid}/files/tree")
    assert "docs-mount" in {i["name"] for i in before.json()["items"]}

    r = await client.delete(f"/v1/workspaces/{wid}/mounts/{m['mount_id']}")
    assert r.status_code == 204, r.text

    listed = await client.get(f"/v1/workspaces/{wid}/mounts")
    assert listed.json()["mounts"] == []

    # The imported files are gone too. On a real backend the deleted dest
    # is a not-found (404), so assert its absence the real way: it no longer
    # appears in the root tree, and a direct tree GET on it 404s.
    root_tree = await client.get(f"/v1/workspaces/{wid}/files/tree")
    assert root_tree.status_code == 200
    assert "docs-mount" not in {i["name"] for i in root_tree.json()["items"]}
    gone = await client.get(
        f"/v1/workspaces/{wid}/files/tree", params={"path": "docs-mount"}
    )
    assert gone.status_code == 404


@pytest.mark.asyncio
async def test_detach_missing_mount_404(client, wsr) -> None:
    wid = await _setup_workspace(client, wsr)
    r = await client.delete(f"/v1/workspaces/{wid}/mounts/wsmnt-does-not-exist")
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_detach_modified_requires_force(client, wsr) -> None:
    wid = await _setup_workspace(client, wsr)
    coll_id = await _setup_collection(client)
    m = (
        await client.post(
            f"/v1/workspaces/{wid}/mounts",
            json={"collection_id": coll_id, "dest": "docs-mount"},
        )
    ).json()
    dest = m["dest"]
    doc_path = m["base"][0]["path"]

    edit = await client.put(
        f"/v1/workspaces/{wid}/files",
        params={"path": f"{dest}/{doc_path}"},
        json={"content": "CHANGED", "encoding": "text"},
    )
    assert edit.status_code == 204, edit.text

    r = await client.delete(f"/v1/workspaces/{wid}/mounts/{m['mount_id']}")
    assert r.status_code == 409, r.text
    ext = r.json()["extensions"]
    assert ext["modified"] is True
    assert doc_path in ext["changed"]

    r2 = await client.delete(
        f"/v1/workspaces/{wid}/mounts/{m['mount_id']}", params={"force": "true"}
    )
    assert r2.status_code == 204, r2.text
    assert (await client.get(f"/v1/workspaces/{wid}/mounts")).json()["mounts"] == []


@pytest.mark.asyncio
async def test_import_empty_collection_seeds_gitkeep(client, wsr) -> None:
    """Mounting a collection with ZERO documents still materialises the dest
    as a real, decorated directory holding a ``.gitkeep`` placeholder, and
    detaches cleanly."""
    wid = await _setup_workspace(client, wsr)
    coll_id = await _make_collection(client, "kb-empty")  # no documents

    r = await client.post(
        f"/v1/workspaces/{wid}/mounts",
        json={"collection_id": coll_id, "dest": "empty-mount"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["collection_id"] == coll_id
    assert body["dest"] == "empty-mount"
    assert body["base"] == []  # no documents -> empty base snapshot

    # Root tree: the dest exists as a collection-origin dir.
    root_tree = await client.get(f"/v1/workspaces/{wid}/files/tree")
    assert root_tree.status_code == 200, root_tree.text
    dest_items = [i for i in root_tree.json()["items"] if i["name"] == "empty-mount"]
    assert len(dest_items) == 1
    assert dest_items[0]["is_dir"] is True
    assert dest_items[0]["origin"] == "collection"

    # The dest dir holds the .gitkeep placeholder.
    tree = await client.get(
        f"/v1/workspaces/{wid}/files/tree", params={"path": "empty-mount"}
    )
    assert tree.status_code == 200, tree.text
    assert {i["name"] for i in tree.json()["items"]} == {".gitkeep"}

    # Detach clean: 204, dest gone.
    d = await client.delete(f"/v1/workspaces/{wid}/mounts/{body['mount_id']}")
    assert d.status_code == 204, d.text
    assert (await client.get(f"/v1/workspaces/{wid}/mounts")).json()["mounts"] == []
    gone = await client.get(
        f"/v1/workspaces/{wid}/files/tree", params={"path": "empty-mount"}
    )
    assert gone.status_code == 404


@pytest.mark.asyncio
async def test_mount_survives_dest_deleted_out_of_band(client, wsr) -> None:
    """A mounted dir is an ordinary workspace directory -- the Studio
    "Delete folder" action or an agent DELETE /files can remove it out from
    under the mount, leaving a stale manifest entry. GET /mounts must
    tolerate the missing dest (not 500), and force-delete must still clean
    up the manifest."""
    wid = await _setup_workspace(client, wsr)
    coll_id = await _setup_collection(client)
    m = (
        await client.post(
            f"/v1/workspaces/{wid}/mounts",
            json={"collection_id": coll_id, "dest": "docs-mount"},
        )
    ).json()

    # Remove the mounted dir out-of-band via the plain files API.
    rm = await client.delete(
        f"/v1/workspaces/{wid}/files",
        params={"path": "docs-mount", "recursive": "true"},
    )
    assert rm.status_code == 204, rm.text

    # GET /mounts must not 500 -- the stale entry reads as fully deleted.
    listed = await client.get(f"/v1/workspaces/{wid}/mounts")
    assert listed.status_code == 200, listed.text
    mounts = listed.json()["mounts"]
    assert len(mounts) == 1
    assert mounts[0]["mount_id"] == m["mount_id"]
    assert mounts[0]["dirty"] is True  # base has files, local is empty

    # Force-delete cleans up the now-orphaned manifest entry.
    d = await client.delete(
        f"/v1/workspaces/{wid}/mounts/{m['mount_id']}", params={"force": "true"}
    )
    assert d.status_code == 204, d.text
    assert (await client.get(f"/v1/workspaces/{wid}/mounts")).json()["mounts"] == []
