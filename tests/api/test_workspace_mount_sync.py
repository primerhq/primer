"""Route tests for the mount sync-back endpoints (Task 7): diff + apply.

Reuses Task 5's fixture harness verbatim (see ``tests/api/test_workspace_mounts.py``)
so the ``/mounts`` endpoints and the ``/files`` endpoints operate on the SAME
fake workspace — editing files via ``PUT /files`` must be visible to
``mount_sync.gather_local`` when the diff/apply endpoints read the mount's
``dest`` directory back.
"""
from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport

from primer.api.app import create_test_app
from primer.api.registries import ProviderRegistry, WorkspaceRegistry
from tests.api.test_workspace_mounts import (
    _MountFakeBackend,
    _setup_collection,
    _setup_workspace,
)
from tests.conftest import _FakeStorageProvider


# ===========================================================================
# Fixtures (identical wiring to test_workspace_mounts.py)
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
# Helpers
# ===========================================================================


async def _setup_mount(client, wsr, dest: str = "coll") -> tuple[str, str, str]:
    """Workspace + 2-doc collection (a.md/b.md), mounted at ``dest``.

    Returns (workspace_id, collection_id, mount_id).
    """
    wid = await _setup_workspace(client, wsr)
    coll_id = await _setup_collection(client)  # a.md="alpha", b.md="beta"

    r = await client.post(
        f"/v1/workspaces/{wid}/mounts",
        json={"collection_id": coll_id, "dest": dest},
    )
    assert r.status_code == 201, r.text
    return wid, coll_id, r.json()["mount_id"]


# ===========================================================================
# GET /v1/workspaces/{id}/mounts/{mount_id}/diff
# ===========================================================================


@pytest.mark.asyncio
async def test_diff_missing_mount_404(client, wsr) -> None:
    wid = await _setup_workspace(client, wsr)
    r = await client.get(f"/v1/workspaces/{wid}/mounts/wsmnt-does-not-exist/diff")
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_diff_clean_mount_is_empty(client, wsr) -> None:
    wid, _coll_id, mount_id = await _setup_mount(client, wsr)
    r = await client.get(f"/v1/workspaces/{wid}/mounts/{mount_id}/diff")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added"] == []
    assert body["modified"] == []
    assert body["deleted"] == []
    assert body["conflicts"] == []
    assert body["orphaned"] is False


@pytest.mark.asyncio
async def test_diff_reflects_local_edits(client, wsr) -> None:
    wid, _coll_id, mount_id = await _setup_mount(client, wsr, dest="coll")

    # Edit a.md, add c.md, delete b.md -- all via the /files router, which
    # must hit the same fake workspace as the /mounts router.
    edit = await client.put(
        f"/v1/workspaces/{wid}/files",
        params={"path": "coll/a.md"},
        json={"content": "A2", "encoding": "text"},
    )
    assert edit.status_code == 204, edit.text

    add = await client.put(
        f"/v1/workspaces/{wid}/files",
        params={"path": "coll/c.md"},
        json={"content": "C", "encoding": "text"},
    )
    assert add.status_code == 204, add.text

    delete = await client.delete(
        f"/v1/workspaces/{wid}/files", params={"path": "coll/b.md"}
    )
    assert delete.status_code == 204, delete.text

    r = await client.get(f"/v1/workspaces/{wid}/mounts/{mount_id}/diff")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["modified"] == ["a.md"]
    assert body["added"] == ["c.md"]
    assert body["deleted"] == ["b.md"]
    assert body["conflicts"] == []
    assert body["orphaned"] is False


# ===========================================================================
# POST /v1/workspaces/{id}/mounts/{mount_id}/apply
# ===========================================================================


@pytest.mark.asyncio
async def test_apply_missing_mount_404(client, wsr) -> None:
    wid = await _setup_workspace(client, wsr)
    r = await client.post(f"/v1/workspaces/{wid}/mounts/wsmnt-does-not-exist/apply")
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_apply_pushes_local_changes_to_collection(client, wsr) -> None:
    wid, coll_id, mount_id = await _setup_mount(client, wsr, dest="coll")

    await client.put(
        f"/v1/workspaces/{wid}/files",
        params={"path": "coll/a.md"},
        json={"content": "A2", "encoding": "text"},
    )
    await client.put(
        f"/v1/workspaces/{wid}/files",
        params={"path": "coll/c.md"},
        json={"content": "C", "encoding": "text"},
    )
    await client.delete(f"/v1/workspaces/{wid}/files", params={"path": "coll/b.md"})

    apply_r = await client.post(f"/v1/workspaces/{wid}/mounts/{mount_id}/apply")
    assert apply_r.status_code == 200, apply_r.text
    result = apply_r.json()
    assert result["applied"] == {"added": 1, "modified": 1, "deleted": 1}
    assert result["failures"] == []

    # Verify upstream: this is the genuine integration assertion -- the
    # collection itself (via DocumentService) reflects the local edits.
    a = await client.get(
        f"/v1/collections/{coll_id}/documents", params={"path": "a.md"}
    )
    assert a.status_code == 200, a.text
    assert a.json()["content"] == "A2"

    c = await client.get(
        f"/v1/collections/{coll_id}/documents", params={"path": "c.md"}
    )
    assert c.status_code == 200, c.text
    assert c.json()["content"] == "C"

    b = await client.get(
        f"/v1/collections/{coll_id}/documents", params={"path": "b.md"}
    )
    assert b.status_code == 404, b.text

    # The mount base is refreshed -- a second diff is clean.
    diff2 = await client.get(f"/v1/workspaces/{wid}/mounts/{mount_id}/diff")
    assert diff2.status_code == 200, diff2.text
    body2 = diff2.json()
    assert body2["added"] == []
    assert body2["modified"] == []
    assert body2["deleted"] == []
    assert body2["conflicts"] == []


# ===========================================================================
# Orphaned mount (upstream collection deleted)
# ===========================================================================


@pytest.mark.asyncio
async def test_diff_orphaned_when_collection_deleted(client, wsr) -> None:
    wid, coll_id, mount_id = await _setup_mount(client, wsr, dest="coll")

    deleted = await client.delete(f"/v1/collections/{coll_id}")
    assert deleted.status_code == 204, deleted.text

    r = await client.get(f"/v1/workspaces/{wid}/mounts/{mount_id}/diff")
    assert r.status_code == 200, r.text
    assert r.json()["orphaned"] is True


@pytest.mark.asyncio
async def test_apply_blocked_when_collection_deleted(client, wsr) -> None:
    wid, coll_id, mount_id = await _setup_mount(client, wsr, dest="coll")

    deleted = await client.delete(f"/v1/collections/{coll_id}")
    assert deleted.status_code == 204, deleted.text

    r = await client.post(f"/v1/workspaces/{wid}/mounts/{mount_id}/apply")
    assert r.status_code == 409, r.text
