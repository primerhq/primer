"""The memory collection of an agent (agent memory Task 2): reserved id, hidden from listings, read through the agent's access, read-only to every
generic door.

The house multi-identity app (tests/api/test_collection_access.py): ALICE owns the agent, BOB reads it, CAROL has full on it, DAVE has nothing, ROOT is
an admin. ``mem-agent-m`` is the memory collection of agent ``agent-m``; it carries no access block of its own.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request

from primer.api.errors import register_error_handlers
from primer.api.routers import knowledge
from primer.knowledge.tree import DocumentTreeService
from primer.memory.collection import memory_collection_id
from primer.model.access import Access
from primer.model.agent import Agent, AgentModel
from primer.model.collection import Collection
from primer.model.principal import Principal
from primer.model.provider import SqliteConfig
from primer.model.user import User
from primer.storage.sqlite import SqliteStorageProvider

ALICE, BOB, CAROL, DAVE, ROOT = "alice", "bob", "carol", "dave", "root"
AGENT = "agent-m"
MID = memory_collection_id(AGENT)
CODE = "memory_collection_read_only"


def _as(uid: str) -> dict[str, str]:
    return {"X-Test-User": f"{uid}:{'admin' if uid == ROOT else 'user'}"}


class _Env:
    def __init__(self, client: httpx.AsyncClient, sp: SqliteStorageProvider, doc_id: str) -> None:
        self.client, self.sp, self.doc_id = client, sp, doc_id


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[_Env]:
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "mem.sqlite"))
    await sp.initialize()
    await sp.get_content_store().ensure_schema()
    try:
        for uid in (ALICE, BOB, CAROL, DAVE, ROOT):
            await sp.get_storage(User).create(User(id=uid, username=uid, created_at=datetime.now(UTC), role="admin" if uid == ROOT else "user"))
        await sp.get_storage(Agent).create(Agent(
            id=AGENT, description="a", model=AgentModel(profile_id="prof"),
            access=Access(owners=[ALICE], grants={BOB: "read", CAROL: "full"}),
        ))
        await sp.get_storage(Collection).create(Collection(id="kb", description="wiki", access=Access(owners=[ALICE], public=True)))
        await sp.get_storage(Collection).create(Collection(id=MID, description="memory", memory_of=AGENT))
        doc = await DocumentTreeService(sp).create(collection_id=MID, parent="", slug="active", body="")
        entry = await DocumentTreeService(sp).create(collection_id=MID, parent="active", slug="retry", body="## Steps\n- [s1] wait")
        app = FastAPI()
        register_error_handlers(app)
        app.state.storage_provider = sp
        app.state.provider_registry = object()
        app.state.semantic_search_registry = object()

        @app.middleware("http")
        async def _actor(request: Request, call_next):
            raw = request.headers.get("X-Test-User")
            if raw:
                uid, role = raw.split(":")
                request.state.actor = Principal(type="user", id=uid, display=uid, role=role, source="local", user_id=uid)
            else:
                request.state.actor = None
            return await call_next(request)

        app.include_router(knowledge.collection_access_router, prefix="/v1")
        app.include_router(knowledge.collection_router, prefix="/v1")
        app.include_router(knowledge.document_router, prefix="/v1")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            yield _Env(client, sp, entry.id)
    finally:
        await sp.aclose()


async def test_mem_prefix_is_reserved_on_create(env: _Env) -> None:
    for uid in (ALICE, ROOT):
        r = await env.client.post("/v1/collections", headers=_as(uid), json={"id": "mem-x", "description": "d"})
        assert r.status_code == 422, r.text


async def test_a_client_cannot_set_memory_of_on_create_or_update(env: _Env) -> None:
    r = await env.client.post("/v1/collections", headers=_as(ALICE), json={"id": "mine", "description": "d", "memory_of": AGENT})
    assert r.status_code == 403, r.text
    mine = await env.client.post("/v1/collections", headers=_as(ALICE), json={"id": "mine", "description": "d"})
    assert mine.status_code == 201, mine.text
    r = await env.client.put("/v1/collections/mine", headers=_as(ALICE), json={**mine.json(), "memory_of": AGENT})
    assert r.status_code == 403, r.text


@pytest.mark.parametrize("uid", [ALICE, BOB, ROOT])
async def test_memory_collection_absent_from_rest_list_and_find(env: _Env, uid: str) -> None:
    listed = await env.client.get("/v1/collections", headers=_as(uid), params={"limit": 200})
    assert listed.status_code == 200, listed.text
    ids = [c["id"] for c in listed.json()["items"]]
    assert "kb" in ids and MID not in ids
    found = await env.client.post("/v1/collections/find", headers=_as(uid), json={"predicate": None, "page": {"kind": "offset", "offset": 0, "length": 50}})
    assert found.status_code == 200, found.text
    assert MID not in [c["id"] for c in found.json()["items"]]


@pytest.mark.parametrize("uid", [ALICE, BOB, CAROL, ROOT])
async def test_a_person_with_read_on_the_agent_reads_the_memory_collection_by_id(env: _Env, uid: str) -> None:
    one = await env.client.get(f"/v1/collections/{MID}/docs", headers=_as(uid), params={"path": "active/retry"})
    assert one.status_code == 200, (uid, one.text)
    assert "wait" in one.json()["body"]
    tree = await env.client.get(f"/v1/collections/{MID}/docs", headers=_as(uid))
    assert tree.status_code == 200, tree.text
    by_id = await env.client.get(f"/v1/documents/{env.doc_id}", headers=_as(uid))
    assert by_id.status_code == 200, by_id.text


async def test_a_stranger_gets_not_found(env: _Env) -> None:
    r = await env.client.get(f"/v1/collections/{MID}/docs", headers=_as(DAVE), params={"path": "active/retry"})
    assert r.status_code == 404, r.text
    assert (await env.client.get(f"/v1/documents/{env.doc_id}", headers=_as(DAVE))).status_code == 404


def _writes(env: _Env) -> list[tuple[str, str, dict]]:
    base = f"/v1/collections/{MID}"
    return [
        ("POST", f"{base}/docs", {"json": {"parent": "", "slug": "x", "body": "b"}}),
        ("PATCH", f"{base}/docs", {"params": {"path": "active/retry"}, "json": {"body": "b"}}),
        ("POST", f"{base}/docs/move", {"json": {"path": "active/retry", "new_parent": "", "new_slug": "z"}}),
        ("DELETE", f"{base}/docs", {"params": {"path": "active/retry"}}),
        ("PUT", f"{base}/documents", {"params": {"path": "x"}, "json": {"content": "b"}}),
        ("DELETE", f"{base}/documents", {"params": {"path": "active/retry"}}),
        ("POST", f"{base}/documents/move", {"json": {"from": "active/retry", "to": "z"}}),
        ("PUT", f"{base}/search", {"json": {"embedder": {"provider_id": "e", "model": "m"}, "vector_store_provider_id": "v"}}),
        ("DELETE", f"{base}/search", {}),
        ("POST", "/v1/documents", {"json": {"collection_id": MID, "slug": "x", "path": "x"}}),
        ("PUT", f"/v1/documents/{env.doc_id}", {"json": {"id": env.doc_id, "collection_id": MID, "slug": "retry", "path": "active/retry", "title": "t"}}),
        ("DELETE", f"/v1/documents/{env.doc_id}", {}),
        ("PUT", f"/v1/collections/{MID}", {"json": {"id": MID, "description": "changed"}}),
        ("DELETE", f"/v1/collections/{MID}", {}),
    ]


@pytest.mark.parametrize("uid", [ALICE, CAROL, ROOT])
async def test_every_generic_write_door_refuses_even_the_agent_owner_and_admins(env: _Env, uid: str) -> None:
    for method, url, kw in _writes(env):
        r = await env.client.request(method, url, headers=_as(uid), **kw)
        assert r.status_code == 403, (uid, method, url, r.status_code, r.text[:200])
        assert r.json()["extensions"]["error"] == CODE, (uid, method, url, r.text[:200])
    assert (await DocumentTreeService(env.sp).read(collection_id=MID, path="active/retry")).body.startswith("## Steps")


async def test_a_read_only_holder_of_the_agent_is_refused_the_same_way(env: _Env) -> None:
    r = await env.client.post(f"/v1/collections/{MID}/docs", headers=_as(BOB), json={"parent": "", "slug": "x", "body": "b"})
    assert r.status_code == 403 and r.json()["extensions"]["error"] == CODE
