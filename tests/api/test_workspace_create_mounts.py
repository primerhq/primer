"""Create-time collection mounts (Task 6).

``POST /v1/workspaces`` accepts an optional
``mounts: list[{"collection_id": ..., "dest": ...}]``. ``create_workspace``
expands each mount's collection into ``overrides.files`` (as
``_DocumentSource`` FileMounts via ``expand_collection`` — resolved by the
EXISTING materialisation ``document_resolver``, no new resolver wiring
needed) and, after ``registry.materialise(...)`` returns the live handle,
writes a ``.state/mounts.json`` manifest so the mount is discoverable via
``GET /workspaces/{id}/mounts``.

Test strategy (see ``.superpowers/sdd/task-6-brief.md`` controller note):
``tests/api/test_workspaces.py``'s ``_FakeBackend.create()`` ignores the
``overrides``/``resolvers`` it is passed — it's a fully in-memory fake with
no real file materialisation — so seeded document files do NOT reliably
appear in the fake workspace fs. The robust, end-to-end assertion is
therefore ``GET /workspaces/{id}/mounts``, which proves the create-time
manifest write happened via the real ``create_workspace`` handler.

We reuse ``test_workspaces.py``'s ``_FakeBackend`` (in-memory workspace
backend) for the WorkspaceRegistry, but need a REAL sqlite-backed
StorageProvider (as in ``test_knowledge_documents_by_path.py``) because
``DocumentService`` (used by ``expand_collection``/``build_base_snapshot``
via ``get_document_service``) requires ``transaction()`` +
``get_content_store()``, neither of which the pure in-memory ``_SP`` fake
in ``test_workspaces.py`` implements.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from primer.api.app import create_test_app
from primer.api.registries import ProviderRegistry, WorkspaceRegistry
from primer.model.collection import Collection, CollectionEmbedder
from primer.model.provider import (
    SqliteConfig,
    StorageProviderConfig,
    StorageProviderType,
)
from primer.model.workspace import (
    LocalWorkspaceConfig,
    WorkspaceProvider,
    WorkspaceProviderType,
    WorkspaceTemplate,
)
from primer.storage.factory import StorageProviderFactory
from tests.api.test_workspaces import _FakeBackend


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


@pytest_asyncio.fixture
async def provider(tmp_path):
    cfg = StorageProviderConfig(
        provider=StorageProviderType.SQLITE,
        config=SqliteConfig(path=tmp_path / "mounts.sqlite"),
    )
    sp = StorageProviderFactory.create(cfg)
    await sp.initialize()
    await sp.get_content_store().ensure_schema()
    yield sp
    await sp.aclose()


@pytest.fixture
def pr(provider) -> ProviderRegistry:
    return ProviderRegistry(
        provider,  # type: ignore[arg-type]
        llm_factory=lambda p: object(),
        embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(),
        toolset_factory=lambda t: object(),
    )


@pytest.fixture
def wsr(provider) -> WorkspaceRegistry:
    # _FakeBackend (imported from test_workspaces.py) is a fully in-memory
    # workspace backend — create() ignores overrides/resolvers entirely, so
    # this keeps workspace creation cheap while the collection/document
    # storage above is real (sqlite).
    return WorkspaceRegistry(provider, factory=_FakeBackend)  # type: ignore[arg-type]


@pytest_asyncio.fixture
async def app(provider, pr, wsr):
    _app = create_test_app(
        storage_provider=provider,  # type: ignore[arg-type]
        provider_registry=pr,
        workspace_registry=wsr,
    )
    if getattr(_app.state, "seed_artifact_default", None) is not None:
        await _app.state.seed_artifact_default()
    yield _app


@pytest_asyncio.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
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


@pytest_asyncio.fixture
async def template_id(client) -> str:
    provider_body = WorkspaceProvider(
        id="local-1",
        provider=WorkspaceProviderType.LOCAL,
        config=LocalWorkspaceConfig(root_path="/tmp/primer-ws-mount-tests"),
    ).model_dump(mode="json")
    post = await client.post("/v1/workspace_providers", json=provider_body)
    assert post.status_code == 201, post.text

    template_body = WorkspaceTemplate(
        id="tpl-1",
        description="dev workspace",
        provider_id="local-1",
    ).model_dump(mode="json")
    r = await client.post("/v1/workspace_templates", json=template_body)
    assert r.status_code == 201, r.text
    return "tpl-1"


@pytest_asyncio.fixture
async def coll_id(client) -> str:
    await client.post("/v1/ssp", json=_SSP_BODY)
    body = Collection(
        id="kb-mount-1",
        description="mount-src",
        embedder=CollectionEmbedder(provider_id="hf-1", model="all-MiniLM-L6-v2"),
        search_provider_id="ssp-test",
    ).model_dump(mode="json")
    created = await client.post("/v1/collections", json=body)
    assert created.status_code == 201, created.text

    # Seed a couple of documents so expand_collection / build_base_snapshot
    # have something to iterate over.
    for path, content in (("a/one.md", "hello one"), ("two.md", "hello two")):
        put = await client.put(
            "/v1/collections/kb-mount-1/documents",
            params={"path": path},
            json={"content": content},
        )
        assert put.status_code in (200, 201), put.text
    return "kb-mount-1"


# ===========================================================================
# Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_create_workspace_with_mount_lists_via_get_mounts(
    client, template_id, coll_id
):
    r = await client.post(
        "/v1/workspaces",
        json={"template_id": template_id, "mounts": [{"collection_id": coll_id}]},
    )
    assert r.status_code == 201, r.text
    ws_id = r.json()["id"]

    mounts_resp = await client.get(f"/v1/workspaces/{ws_id}/mounts")
    assert mounts_resp.status_code == 200, mounts_resp.text
    mounts = mounts_resp.json()["mounts"]
    assert len(mounts) == 1
    entry = mounts[0]
    assert entry["collection_id"] == coll_id
    assert entry["dest"]  # non-empty, sanitized dest
    assert entry["mount_id"].startswith("wsmnt-")
    assert {b["path"] for b in entry["base"]} == {"a/one.md", "two.md"}


@pytest.mark.asyncio
async def test_create_workspace_with_mount_honors_custom_dest(
    client, template_id, coll_id
):
    r = await client.post(
        "/v1/workspaces",
        json={
            "template_id": template_id,
            "mounts": [{"collection_id": coll_id, "dest": "my-docs"}],
        },
    )
    assert r.status_code == 201, r.text
    ws_id = r.json()["id"]

    mounts = (await client.get(f"/v1/workspaces/{ws_id}/mounts")).json()["mounts"]
    assert len(mounts) == 1
    assert mounts[0]["dest"] == "my-docs"


@pytest.mark.asyncio
async def test_create_workspace_without_mounts_is_unaffected(client, template_id):
    """Regression guard: the no-mounts create path is unchanged — the
    workspace still creates fine and no manifest is written (GET /mounts
    returns an empty list rather than erroring)."""
    r = await client.post("/v1/workspaces", json={"template_id": template_id})
    assert r.status_code == 201, r.text
    ws_id = r.json()["id"]

    mounts_resp = await client.get(f"/v1/workspaces/{ws_id}/mounts")
    assert mounts_resp.status_code == 200, mounts_resp.text
    assert mounts_resp.json()["mounts"] == []


@pytest.mark.asyncio
async def test_create_workspace_mount_missing_collection_is_404(client, template_id):
    r = await client.post(
        "/v1/workspaces",
        json={
            "template_id": template_id,
            "mounts": [{"collection_id": "does-not-exist"}],
        },
    )
    assert r.status_code == 404, r.text
