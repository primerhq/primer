"""Agent memory entry routes and the activity feed (agent memory Task 12).

The house multi-identity app: ALICE owns the agent, BOB reads it, CAROL has full on it, DAVE has nothing, ROOT is an admin.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request

from primer.api.errors import register_error_handlers
from primer.api.routers import agent_memory, compute
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind
from primer.knowledge.tree import DocumentTreeService
from primer.events.recorder import recorder_for
from primer.knowledge.document_service import DocumentService
from primer.memory.collection import memory_collection_id
from primer.memory.store import MemoryStore
from primer.memory.prompts import DEFAULT_REFLECTION_PROMPT
from primer.model.access import Access
from primer.model.agent import Agent, AgentModel
from primer.model.collection import Collection
from primer.model.memory import AgentMemory, MemoryAdd, MemoryJob
from primer.model.principal import Principal
from primer.model.provider import SqliteConfig
from primer.model.storage import OffsetPage
from primer.model.user import User
from primer.storage.sqlite import SqliteStorageProvider

ALICE, BOB, CAROL, DAVE, ROOT = "alice", "bob", "carol", "dave", "root"
AGENT = "agent-m"
MID = memory_collection_id(AGENT)
BASE = f"/v1/agents/{AGENT}/memory"


def _as(uid: str) -> dict[str, str]:
    return {"X-Test-User": f"{uid}:{'admin' if uid == ROOT else 'user'}"}


class _Env:
    def __init__(self, client: httpx.AsyncClient, sp: SqliteStorageProvider, engine: InMemoryClaimEngine) -> None:
        self.client, self.sp, self.engine = client, sp, engine
        self.store = MemoryStore(sp, DocumentService(sp), recorder_for(sp))

    async def seed(self, name: str = "Retry GitHub 429", *, status: str = "candidates", needs_approval: bool = False) -> str:
        add = MemoryAdd(op="add", name=name, description="d", steps=["wait", "retry"], pitfalls=["no hammering"], example=None, topics=["github"])
        entry = await self.store.add_candidate(AGENT, add, source={"session_id": "s", "from_turn": 1, "to_turn": 2}, taught_by="alice", needs_approval=needs_approval)
        if status == "active":
            await self.store.approve(AGENT, entry.name, actor="user:alice")
        return entry.name

    async def jobs(self) -> list[MemoryJob]:
        return list((await self.sp.get_storage(MemoryJob).list(OffsetPage(offset=0, length=100))).items)


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[_Env]:
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "mem.sqlite"))
    await sp.initialize()
    await sp.get_content_store().ensure_schema()
    await sp.get_event_store().ensure_schema()
    await sp.get_storage(Collection).create(Collection(id=MID, description="memory", memory_of=AGENT))
    try:
        for uid in (ALICE, BOB, CAROL, DAVE, ROOT):
            await sp.get_storage(User).create(User(id=uid, username=uid, created_at=datetime.now(UTC), role="admin" if uid == ROOT else "user"))
        await sp.get_storage(Agent).create(Agent(
            id=AGENT, description="a", model=AgentModel(profile_id="prof"),
            access=Access(owners=[ALICE], grants={BOB: "read", CAROL: "full"}),
        ))
        engine = InMemoryClaimEngine(adapters={})
        app = FastAPI()
        register_error_handlers(app)
        app.state.storage_provider = sp
        app.state.provider_registry = object()
        app.state.semantic_search_registry = None
        app.state.claim_engine = engine

        @app.middleware("http")
        async def _actor(request: Request, call_next):
            raw = request.headers.get("X-Test-User")
            if raw:
                uid, role = raw.split(":")
                request.state.actor = Principal(type="user", id=uid, display=uid, role=role, source="local", user_id=uid)
            else:
                request.state.actor = None
            return await call_next(request)

        app.include_router(agent_memory.agent_memory_router, prefix="/v1")
        app.include_router(compute.agent_access_router, prefix="/v1")
        app.include_router(compute.agent_router, prefix="/v1")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            yield _Env(client, sp, engine)
    finally:
        await sp.aclose()


async def test_reader_lists_and_views_entries(env: _Env) -> None:
    await env.seed("Retry GitHub 429")
    await env.seed("Pin me", status="active")
    r = await env.client.get(f"{BASE}/entries", headers=_as(BOB))
    assert r.status_code == 200, r.text
    assert sorted((e["name"], e["status"]) for e in r.json()["items"]) == [("pin-me", "active"), ("retry-github-429", "candidates")]
    r = await env.client.get(f"{BASE}/entries?status=active", headers=_as(BOB))
    assert [e["name"] for e in r.json()["items"]] == ["pin-me"]
    one = await env.client.get(f"{BASE}/entries/retry-github-429", headers=_as(BOB))
    body = one.json()
    assert one.status_code == 200 and body["body"]["steps"] == [{"id": "s1", "text": "wait"}, {"id": "s2", "text": "retry"}]
    assert body["meta"]["taught_by"] == "alice" and body["description"] == "d"
    assert (await env.client.get(f"{BASE}/entries/nope", headers=_as(BOB))).status_code == 404
    assert (await env.client.get(f"{BASE}/entries", headers=_as(DAVE))).status_code == 404, "an agent a person cannot read does not exist to them"
    assert (await env.client.get(f"{BASE}/entries?status=bogus", headers=_as(BOB))).status_code == 422


async def test_owner_only_mutations_refuse_full_grantee(env: _Env) -> None:
    name = await env.seed()
    calls = [
        ("patch", f"{BASE}/entries/{name}", {"description": "x"}),
        ("post", f"{BASE}/entries/{name}/approve", None),
        ("post", f"{BASE}/entries/{name}/archive", None),
        ("post", f"{BASE}/purge", None),
        ("get", f"{BASE}/activity", None),
    ]
    for uid in (CAROL, BOB):
        for method, url, body in calls:
            r = await env.client.request(method.upper(), url, headers=_as(uid), **({"json": body} if body is not None else {}))
            assert r.status_code == 403, (uid, method, url, r.text)
            assert r.json()["extensions"]["error"] == "memory_owner_only"
    for method, url, body in calls:
        r = await env.client.request(method.upper(), url, headers=_as(DAVE), **({"json": body} if body is not None else {}))
        assert r.status_code == 404, (method, url, r.status_code)
    assert (await env.store.get(AGENT, name)).status == "candidates" and (await env.store.get(AGENT, name)).description == "d"


async def test_approve_archive_pin_edit_purge_round_trip(env: _Env) -> None:
    name = await env.seed(needs_approval=True)
    assert (await env.client.post(f"{BASE}/entries/{name}/approve", headers=_as(ALICE))).json()["status"] == "active"
    r = await env.client.patch(f"{BASE}/entries/{name}", headers=_as(ALICE), json={
        "description": "new d", "pinned": True, "body": [{"action": "change", "section": "steps", "item_id": "s1", "text": "wait longer"}],
    })
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["description"] == "new d" and got["meta"]["pinned"] is True and got["body"]["steps"][0] == {"id": "s1", "text": "wait longer"}
    assert len(got["meta"]["revisions"]) == 1
    missing = await env.client.patch(f"{BASE}/entries/nope", headers=_as(ALICE), json={"description": "x"})
    assert missing.status_code == 404
    assert (await env.client.post(f"{BASE}/entries/{name}/archive", headers=_as(ROOT))).json()["status"] == "archived"
    other = await env.seed("Another")
    purged = await env.client.post(f"{BASE}/purge", headers=_as(ALICE))
    assert purged.status_code == 200 and purged.json() == {"removed": 2}, purged.text
    assert await env.store.list_entries(AGENT) == []
    assert await env.sp.get_storage(Collection).get(MID) is not None, "the collection stays"
    assert (await env.client.post(f"{BASE}/entries/{other}/approve", headers=_as(ALICE))).status_code == 404


async def test_edit_over_body_cap_is_422(env: _Env) -> None:
    name = await env.seed()
    r = await env.client.patch(f"{BASE}/entries/{name}", headers=_as(ALICE), json={
        "body": [{"action": "add", "section": "steps", "text": "x" * 3300}],
    })
    assert r.status_code == 422, r.text
    assert len((await env.store.get(AGENT, name)).body.steps) == 2


async def test_activity_lists_memory_events_newest_first(env: _Env) -> None:
    name = await env.seed()
    await env.client.post(f"{BASE}/entries/{name}/approve", headers=_as(ALICE))
    await env.client.post(f"{BASE}/entries/{name}/archive", headers=_as(ALICE))
    await env.sp.get_event_store().append(event_type="agent.updated", entity_kind="agent", entity_id=AGENT)
    r = await env.client.get(f"{BASE}/activity", headers=_as(ALICE))
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [i["type"] for i in items] == ["memory.entry.archived", "memory.entry.approved", "memory.entry.added"]
    assert items[0]["actor"] == "user:alice" and items[0]["payload"]["name"] == name
    ids = [i["id"] for i in items]
    assert ids == sorted(ids, reverse=True)
    newer = await env.client.get(f"{BASE}/activity?after={ids[2]}&limit=1", headers=_as(ALICE))
    assert [i["id"] for i in newer.json()["items"]] == [ids[0]], "limit keeps the newest"
