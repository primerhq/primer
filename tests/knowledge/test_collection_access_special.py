"""The special collections and the platform writers of collection rows (access-control plan P3 Task 3, Decisions 7-8).

``system`` is public (every full user reads it; the wiki tools depend on it) and stays write-protected through its system
flag; ``_internal_*`` collections carry no owners and are not public, so only admins reach them; the ``this-install/`` live
map lists only public entities. The platform writers (the search lifecycle, internal-collection materialisation, the
system wiki's title) write their own fields, so an owner's sharing change made in between survives them.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request

from primer.access.person import Person
from primer.access.service import AccessUpdate, set_access
from primer.api.errors import register_error_handlers
from primer.api.routers import knowledge
from primer.internal_collections import InternalCollectionsSubsystem
from primer.knowledge.lifecycle import _write_search
from primer.knowledge.system_collection import (
    SYSTEM_COLLECTION_ID,
    converge_entity,
    regenerate_system_collection,
)
from primer.knowledge.tree import DocumentTreeService
from primer.model.access import Access
from primer.model.agent import Agent, AgentModel
from primer.model.collection import Collection, CollectionEmbedder, CollectionSearchConfig
from primer.model.except_ import NotFoundError
from primer.model.internal import INTERNAL_COLLECTION_IDS
from primer.model.principal import Principal
from primer.model.provider import SqliteConfig
from primer.model.user import User
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.collections import build_collections_toolset
from primer.toolset.system import build_system_toolset
from tests._support.caller import caller
from tests.toolset.test_wiki_tools import _Registry

ALICE, BOB, ROOT = "alice", "bob", "root"
INTERNAL = INTERNAL_COLLECTION_IDS["agent"]


def _as(uid: str) -> dict[str, str]:
    return {"X-Test-User": f"{uid}:{'admin' if uid == ROOT else 'user'}"}


@pytest_asyncio.fixture
async def sp(tmp_path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await provider.initialize()
    await provider.get_content_store().ensure_schema()
    for uid in (ALICE, BOB, ROOT):
        await provider.get_storage(User).create(
            User(id=uid, username=uid, created_at=datetime.now(UTC), role="admin" if uid == ROOT else "user")
        )
    yield provider
    await provider.aclose()


async def _materialise(sp) -> None:
    """The bootstrap phase that writes the ``_internal_*`` rows (it reads nothing but the storage provider)."""
    await InternalCollectionsSubsystem._materialise_collection_rows(SimpleNamespace(_sp=sp))


@pytest_asyncio.fixture
async def client(sp) -> AsyncIterator[httpx.AsyncClient]:
    await regenerate_system_collection(sp, toolset_providers={})
    await _materialise(sp)
    app = FastAPI()
    register_error_handlers(app)
    app.state.storage_provider = sp
    app.state.provider_registry = object()
    app.state.semantic_search_registry = object()

    @app.middleware("http")
    async def _actor(request: Request, call_next):
        uid, role = request.headers["X-Test-User"].split(":")
        request.state.actor = Principal(type="user", id=uid, display=uid, role=role, source="local", user_id=uid)
        return await call_next(request)

    app.include_router(knowledge.collection_access_router, prefix="/v1")
    app.include_router(knowledge.collection_router, prefix="/v1")
    app.include_router(knowledge.document_router, prefix="/v1")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


# --------------------------------------------------------------------------------------------------------------------
# system: public, read by everyone, written by no one
# --------------------------------------------------------------------------------------------------------------------


async def test_the_system_collection_is_created_public(sp):
    await regenerate_system_collection(sp, toolset_providers={})
    row = await sp.get_storage(Collection).get(SYSTEM_COLLECTION_ID)
    assert row.system is True and row.access.public is True and row.access.owners == []


async def test_regeneration_makes_a_private_system_row_public_and_keeps_its_search_block(sp):
    cfg = CollectionSearchConfig(embedder=CollectionEmbedder(provider_id="e", model="m"), vector_store_provider_id="s")
    await sp.get_storage(Collection).create(
        Collection(id=SYSTEM_COLLECTION_ID, description="Primer system reference", system=True, search=cfg)
    )
    await regenerate_system_collection(sp, toolset_providers={})
    row = await sp.get_storage(Collection).get(SYSTEM_COLLECTION_ID)
    assert row.access.public is True and row.description == "Platform wiki" and row.search == cfg


async def test_a_plain_user_reads_the_system_collection_over_rest(client):
    r = await client.get("/v1/collections", headers=_as(BOB))
    assert SYSTEM_COLLECTION_ID in [c["id"] for c in r.json()["items"]]
    r = await client.get(f"/v1/collections/{SYSTEM_COLLECTION_ID}/docs", headers=_as(BOB), params={"path": "this-install"})
    assert r.status_code == 200 and r.json()["document"]["path"] == "this-install", r.text
    r = await client.get(f"/v1/collections/{SYSTEM_COLLECTION_ID}/docs", headers=_as(BOB), params={"parent": ""})
    assert r.status_code == 200 and r.json()["nodes"], r.text
    r = await client.get(f"/v1/collections/{SYSTEM_COLLECTION_ID}/grep", headers=_as(BOB), params={"q": "install"})
    assert r.status_code == 200, r.text
    r = await client.get("/v1/documents", headers=_as(BOB))
    assert r.status_code == 200 and r.json()["total"] > 0


async def test_a_plain_user_cannot_write_the_system_collection(client):
    for method, url, kw in (
        ("PUT", f"/v1/collections/{SYSTEM_COLLECTION_ID}/documents", {"params": {"path": "x"}, "json": {"content": "x"}}),
        ("POST", f"/v1/collections/{SYSTEM_COLLECTION_ID}/docs", {"json": {"parent": "", "slug": "x", "body": ""}}),
        ("PUT", f"/v1/collections/{SYSTEM_COLLECTION_ID}", {"json": {"id": SYSTEM_COLLECTION_ID, "description": "mine"}}),
        ("DELETE", f"/v1/collections/{SYSTEM_COLLECTION_ID}", {}),
        ("PUT", f"/v1/collections/{SYSTEM_COLLECTION_ID}/access", {"json": {"owners": [BOB]}}),
    ):
        r = await client.request(method, url, headers=_as(BOB), **kw)
        assert r.status_code == 403 and r.json()["extensions"]["code"] == "access_denied", (method, url, r.text)
    # An admin passes the level and still meets the system flag.
    r = await client.put(
        f"/v1/collections/{SYSTEM_COLLECTION_ID}/documents", headers=_as(ROOT), params={"path": "x"}, json={"content": "x"}
    )
    assert r.status_code == 403 and "system-owned and read-only" in r.text


async def test_a_plain_user_reads_the_wiki_through_the_wiki_tools(sp):
    await regenerate_system_collection(sp, toolset_providers={})
    toolset = build_system_toolset(storage_provider=sp, provider_registry=_Registry(build_collections_toolset(storage_provider=sp)))
    user = caller("user")
    r = await toolset.call(tool_name="wiki_read", ctx=user, arguments={"path": "this-install"})
    assert not r.is_error and json.loads(r.output)["path"] == "this-install", r.output
    r = await toolset.call(tool_name="wiki_search", ctx=user, arguments={"query": "install", "mode": "fulltext", "path_prefix": "this-install"})
    assert not r.is_error and json.loads(r.output)["hits"], r.output


# --------------------------------------------------------------------------------------------------------------------
# _internal_*: admin-only
# --------------------------------------------------------------------------------------------------------------------


async def test_internal_collections_are_admin_only(client, sp):
    row = await sp.get_storage(Collection).get(INTERNAL)
    assert row.access == Access()
    listed = [c["id"] for c in (await client.get("/v1/collections", headers=_as(BOB))).json()["items"]]
    assert not [cid for cid in listed if cid.startswith("_internal_")]
    for url in (f"/v1/collections/{INTERNAL}", f"/v1/collections/{INTERNAL}/indexed_documents", f"/v1/collections/{INTERNAL}/search"):
        r = await client.get(url, headers=_as(BOB))
        assert r.status_code == 404 and r.json()["detail"] == f"Collection {INTERNAL!r} does not exist", (url, r.text)
    assert (await client.get(f"/v1/collections/{INTERNAL}", headers=_as(ROOT))).status_code == 200


# --------------------------------------------------------------------------------------------------------------------
# the live map lists only public entities
# --------------------------------------------------------------------------------------------------------------------


def _agent(agent_id: str, access: Access) -> Agent:
    return Agent(id=agent_id, description="helper", model=AgentModel(profile_id="prov--m"), access=access)


async def _read(sp, path: str):
    return await DocumentTreeService(sp).read(collection_id=SYSTEM_COLLECTION_ID, path=path)


async def test_the_live_map_hides_a_private_agent_and_lists_a_public_one(sp):
    await sp.get_storage(Agent).create(_agent("ag-public", Access(owners=[ALICE], public=True)))
    await sp.get_storage(Agent).create(_agent("ag-private", Access(owners=[ALICE])))
    await sp.get_storage(Collection).create(Collection(id="kb-private", description="d", access=Access(owners=[ALICE])))
    await _materialise(sp)
    await regenerate_system_collection(sp, toolset_providers={})

    index = (await _read(sp, "this-install/agents")).body
    assert "ag-public" in index and "ag-private" not in index
    assert "helper" in (await _read(sp, "this-install/agents/ag-public")).body
    with pytest.raises(NotFoundError):
        await _read(sp, "this-install/agents/ag-private")
    collections = (await _read(sp, "this-install/collections")).body
    assert "kb-private" not in collections and "_internal_" not in collections


async def test_an_agent_made_private_leaves_the_map_and_one_made_public_joins_it(sp):
    agents = sp.get_storage(Agent)
    await agents.create(_agent("ag-1", Access(owners=[ALICE], public=True)))
    await regenerate_system_collection(sp, toolset_providers={})
    assert "ag-1" in (await _read(sp, "this-install/agents")).body

    alice = Person(ALICE, "user")
    users = sp.get_storage(User)
    await set_access(agents, users, "ag-1", alice, AccessUpdate(owners=[ALICE]), what="Agent 'ag-1'")
    await converge_entity(sp, entity_type="agent", entity_id="ag-1")
    assert "ag-1" not in (await _read(sp, "this-install/agents")).body
    with pytest.raises(NotFoundError):
        await _read(sp, "this-install/agents/ag-1")

    await set_access(agents, users, "ag-1", alice, AccessUpdate(owners=[ALICE], public=True), what="Agent 'ag-1'")
    await converge_entity(sp, entity_type="agent", entity_id="ag-1")
    assert "ag-1" in (await _read(sp, "this-install/agents")).body

    # A boot regeneration prunes a page whose entity went private meanwhile.
    await set_access(agents, users, "ag-1", alice, AccessUpdate(owners=[ALICE]), what="Agent 'ag-1'")
    await regenerate_system_collection(sp, toolset_providers={})
    with pytest.raises(NotFoundError):
        await _read(sp, "this-install/agents/ag-1")


# --------------------------------------------------------------------------------------------------------------------
# the platform writers keep a concurrent sharing change
# --------------------------------------------------------------------------------------------------------------------


async def test_a_revoke_racing_a_search_write_survives(sp):
    colls = sp.get_storage(Collection)
    await colls.create(Collection(id="kb", description="d", access=Access(owners=[ALICE], grants={BOB: "read"})))
    snapshot = await colls.get("kb")  # the lifecycle read the row ...
    await set_access(colls, sp.get_storage(User), "kb", Person(ALICE, "user"), AccessUpdate(owners=[ALICE]), what="kb")
    cfg = CollectionSearchConfig(embedder=CollectionEmbedder(provider_id="e", model="m"), vector_store_provider_id="s")
    await _write_search(sp, snapshot, cfg)  # ... and writes its search state after the owner revoked bob
    row = await colls.get("kb")
    assert row.search == cfg and row.access.grants == {} and row.access.rev == 1


async def test_a_sharing_change_survives_materialisation(sp):
    await _materialise(sp)
    colls = sp.get_storage(Collection)
    await set_access(colls, sp.get_storage(User), INTERNAL, Person(ROOT, "admin"), AccessUpdate(owners=[ROOT]), what=INTERNAL)
    await _materialise(sp)
    row = await colls.get(INTERNAL)
    assert row.access.owners == [ROOT] and row.system is True


async def test_a_sharing_change_survives_the_system_title_write(sp):
    colls = sp.get_storage(Collection)
    await colls.create(Collection(id=SYSTEM_COLLECTION_ID, description="old", system=True, access=Access(public=True, owners=[ROOT])))
    await regenerate_system_collection(sp, toolset_providers={})
    row = await colls.get(SYSTEM_COLLECTION_ID)
    assert row.description == "Platform wiki" and row.access.owners == [ROOT] and row.access.public is True
