"""Agent memory settings (agent memory Task 5): who can read and change them, what turning memory on sets going, and what deleting the agent removes.

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
from primer.memory.collection import memory_collection_id
from primer.memory.prompts import DEFAULT_REFLECTION_PROMPT
from primer.model.access import Access
from primer.model.agent import Agent, AgentModel
from primer.model.collection import Collection
from primer.model.memory import AgentMemory, MemoryJob
from primer.model.principal import Principal
from primer.model.provider import SqliteConfig
from primer.model.storage import OffsetPage
from primer.model.user import User
from primer.storage.sqlite import SqliteStorageProvider

ALICE, BOB, CAROL, DAVE, ROOT = "alice", "bob", "carol", "dave", "root"
AGENT = "agent-m"
MID = memory_collection_id(AGENT)
URL = f"/v1/agents/{AGENT}/memory/settings"


def _as(uid: str) -> dict[str, str]:
    return {"X-Test-User": f"{uid}:{'admin' if uid == ROOT else 'user'}"}


class _Env:
    def __init__(self, client: httpx.AsyncClient, sp: SqliteStorageProvider, engine: InMemoryClaimEngine) -> None:
        self.client, self.sp, self.engine = client, sp, engine

    async def put(self, uid: str = ALICE, **body):
        return await self.client.put(URL, headers=_as(uid), json=body)

    async def jobs(self) -> list[MemoryJob]:
        return list((await self.sp.get_storage(MemoryJob).list(OffsetPage(offset=0, length=100))).items)


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


async def test_get_settings_defaults_for_agent_reader(env: _Env) -> None:
    r = await env.client.get(URL, headers=_as(BOB))
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["id"], body["enabled"], body["promotion"], body["teachers"], body["idle_minutes"], body["auto_inject"]) == (
        AGENT, False, "auto", "owner", 10, False,
    )
    assert body["default_reflection_prompt"] == DEFAULT_REFLECTION_PROMPT
    assert (await env.client.get(URL, headers=_as(DAVE))).status_code == 404, "an agent a person cannot read does not exist to them"


async def test_put_requires_owner(env: _Env) -> None:
    for uid in (CAROL, BOB):
        r = await env.put(uid, enabled=True)
        assert r.status_code == 403, (uid, r.text)
        assert r.json()["extensions"]["error"] == "memory_owner_only"
    assert (await env.put(DAVE, enabled=True)).status_code == 404
    assert (await env.put(ALICE, enabled=True)).status_code == 200
    assert (await env.put(ROOT, enabled=True, idle_minutes=5)).status_code == 200, "an admin is an owner of everything"
    assert (await env.sp.get_storage(AgentMemory).get(AGENT)).idle_minutes == 5


async def test_put_refuses_unknown_and_server_managed_fields(env: _Env) -> None:
    assert (await env.put(ALICE, enabled=True, surprise=1)).status_code == 422
    assert (await env.put(ALICE, reflections_count=0)).status_code == 422
    assert (await env.put(ALICE, idle_minutes=0)).status_code == 422


async def test_enable_creates_collection_topics_job_and_daily_curate_job(env: _Env) -> None:
    before = datetime.now(UTC)
    r = await env.put(enabled=True)
    assert r.status_code == 200 and r.json()["enabled"] is True, r.text
    coll = await env.sp.get_storage(Collection).get(MID)
    assert coll is not None and coll.memory_of == AGENT
    assert {j.id: j.kind for j in await env.jobs()} == {f"topics:{AGENT}": "topics", f"curate:{AGENT}": "curate"}
    topics_due = env.engine._leases[(ClaimKind.MEMORY, f"topics:{AGENT}")].next_attempt_at
    curate_due = env.engine._leases[(ClaimKind.MEMORY, f"curate:{AGENT}")].next_attempt_at
    assert before - timedelta(seconds=1) <= topics_due <= datetime.now(UTC) + timedelta(seconds=1)
    assert timedelta(hours=23, minutes=59) <= curate_due - before <= timedelta(hours=24, minutes=1)


async def test_enabling_again_does_not_queue_the_jobs_twice(env: _Env) -> None:
    await env.put(enabled=True)
    await env.put(enabled=True, idle_minutes=7)
    assert len(await env.jobs()) == 2


async def test_disable_keeps_entries(env: _Env) -> None:
    await env.put(enabled=True)
    await DocumentTreeService(env.sp).create(collection_id=MID, parent="", slug="active", body="")
    r = await env.put(enabled=False)
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert await env.sp.get_storage(Collection).get(MID) is not None
    assert (await DocumentTreeService(env.sp).read(collection_id=MID, path="active")).document.slug == "active"


async def test_default_reflection_prompt_is_returned(env: _Env) -> None:
    assert len(DEFAULT_REFLECTION_PROMPT) > 200
    assert (await env.client.get(URL, headers=_as(ALICE))).json()["default_reflection_prompt"] == DEFAULT_REFLECTION_PROMPT
    await env.put(reflection_prompt="my own")
    got = (await env.client.get(URL, headers=_as(ALICE))).json()
    assert got["reflection_prompt"] == "my own" and got["default_reflection_prompt"] == DEFAULT_REFLECTION_PROMPT
    await env.put(reflection_prompt=None)
    assert (await env.client.get(URL, headers=_as(ALICE))).json()["reflection_prompt"] is None, "clearing returns to the built-in prompt"


async def test_editing_the_topic_lists_marks_them_edited_and_other_edits_keep_server_fields(env: _Env) -> None:
    store = env.sp.get_storage(AgentMemory)
    await env.put(enabled=True)
    assert (await store.get(AGENT)).topics_edited is False
    await store.patch_if(AGENT, {"reflections_count": 3, "reflections_day": "2026-11-01"}, where={"enabled": [True]})
    await env.put(enabled=True, idle_minutes=20)
    row = await store.get(AGENT)
    assert (row.reflections_count, row.topics_edited, row.idle_minutes) == (3, False, 20), "a settings write leaves the server's counters alone"
    await env.put(enabled=True, idle_minutes=20, capture_topics=["billing errors"])
    row = await store.get(AGENT)
    assert (row.capture_topics, row.topics_edited) == (["billing errors"], True)


async def test_deleting_the_agent_removes_memory(env: _Env) -> None:
    await env.put(enabled=True)
    await DocumentTreeService(env.sp).create(collection_id=MID, parent="", slug="active", body="")
    assert await env.engine.has_lease(ClaimKind.MEMORY, f"topics:{AGENT}")
    r = await env.client.delete(f"/v1/agents/{AGENT}", headers=_as(ALICE))
    assert r.status_code == 204, r.text
    assert await env.sp.get_storage(Collection).get(MID) is None
    assert await env.sp.get_storage(AgentMemory).get(AGENT) is None
    assert await env.jobs() == []
    assert not await env.engine.has_lease(ClaimKind.MEMORY, f"topics:{AGENT}")
    assert not await env.engine.has_lease(ClaimKind.MEMORY, f"curate:{AGENT}")
