"""Harnesses are owned; what they install inherits their access by copy (access-control P3 Task 6, Decisions 1, 2, 8).

A harness's agents, graphs, toolsets and collections carry a copy of the harness's access (documents follow their collection);
sharing the harness fans the new access out to every row with its ``harness_id``; sharing such a row directly is refused
(409 ``harness_managed``). List/get/bundle of a harness need READ; writes stay admin-only. A sync writes an installed row's other
fields and never its access, so a revoke racing a sync survives.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request

from primer.access.person import Person
from primer.access.service import AccessUpdate, set_access
from primer.api.errors import register_error_handlers
from primer.api.routers import compute, harness as harness_routes
from primer.harness.access import apply_harness_access
from primer.harness.outbound import build_outbound
from primer.model.access import Access
from primer.model.agent import Agent
from primer.model.collection import Collection
from primer.model.except_ import ConflictError
from primer.model.graph import Graph
from primer.model.harness import Harness, HarnessDirection, TrackedEntity
from primer.model.principal import Principal, PrincipalRef
from primer.model.provider import SqliteConfig
from primer.model.storage import OffsetPage
from primer.model.user import User
from primer.model.yield_ import ToolContext
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.harness import build_harness_toolset_provider

ALICE, BOB, ROOT = "alice", "bob", "root"
NOW = datetime(2026, 10, 31, tzinfo=UTC)
PRIVATE = Access(owners=[ALICE])


def _as(uid: str) -> dict[str, str]:
    return {"X-Test-User": f"{uid}:{'admin' if uid == ROOT else 'user'}"}


def _harness(access: Access = PRIVATE, **kw: Any) -> Harness:
    return Harness(
        id="hns_1", slug="acme", name="Acme", git_url="https://github.com/x/y", created_at=NOW, access=access, **kw
    )


def _agent(agent_id: str = "ag-1", harness_id: str | None = "hns_1", access: Access = PRIVATE) -> Agent:
    return Agent(
        id=agent_id, description="d", harness_id=harness_id, model={"profile_id": "p--m"}, tools=[], system_prompt=[], access=access
    )


class _Env:
    def __init__(self, client: httpx.AsyncClient, sp: SqliteStorageProvider) -> None:
        self.client = client
        self.sp = sp


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[_Env]:
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "h.sqlite"))
    await sp.initialize()
    try:
        for uid in (ALICE, BOB, ROOT):
            await sp.get_storage(User).create(
                User(id=uid, username=uid, created_at=NOW, role="admin" if uid == ROOT else "user")
            )
        await sp.get_storage(Harness).create(_harness())
        await sp.get_storage(Agent).create(_agent())
        await sp.get_storage(Graph).create(
            Graph(id="gr-1", description="g", harness_id="hns_1", nodes=[], edges=[], access=PRIVATE)
        )
        await sp.get_storage(Collection).create(Collection(id="kb-1", description="k", harness_id="hns_1", access=PRIVATE))
        app = FastAPI()
        register_error_handlers(app)
        app.state.storage_provider = sp
        app.state.provider_registry = object()

        @app.middleware("http")
        async def _actor(request: Request, call_next):
            raw = request.headers.get("X-Test-User")
            if raw:
                uid, role = raw.split(":")
                request.state.actor = Principal(type="user", id=uid, display=uid, role=role, source="local", user_id=uid)
                request.state.user = await sp.get_storage(User).get(uid)
            else:
                request.state.actor = None
            return await call_next(request)

        app.include_router(harness_routes.harness_access_router, prefix="/v1")
        app.include_router(harness_routes.harness_router)
        app.include_router(compute.agent_access_router, prefix="/v1")
        app.include_router(compute.agent_router, prefix="/v1")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            yield _Env(client, sp)
    finally:
        await sp.aclose()


# --- visibility of the harness and of what it installed -----------------------------------------------------------------------


async def test_a_user_without_read_sees_neither_the_harness_nor_its_agents_and_sees_both_after_a_share(env):
    listed = await env.client.get("/v1/harnesses", headers=_as(BOB))
    assert listed.status_code == 200 and listed.json()["items"] == []
    assert (await env.client.get("/v1/harnesses/hns_1", headers=_as(BOB))).status_code == 404
    assert (await env.client.get("/v1/harnesses/hns_1/bundle.tar.gz", headers=_as(BOB))).status_code == 404
    agents = await env.client.get("/v1/agents", headers=_as(BOB))
    assert [a["id"] for a in agents.json()["items"]] == []

    shared = await env.client.put(
        "/v1/harnesses/hns_1/access", headers=_as(ALICE), json={"owners": [ALICE], "grants": {BOB: "read"}, "rev": 0}
    )
    assert shared.status_code == 200, shared.text

    assert [h["id"] for h in (await env.client.get("/v1/harnesses", headers=_as(BOB))).json()["items"]] == ["hns_1"]
    assert (await env.client.get("/v1/harnesses/hns_1", headers=_as(BOB))).status_code == 200
    assert [a["id"] for a in (await env.client.get("/v1/agents", headers=_as(BOB))).json()["items"]] == ["ag-1"]
    # what the share fanned out to
    for model, rid in ((Agent, "ag-1"), (Graph, "gr-1"), (Collection, "kb-1")):
        row = await env.sp.get_storage(model).get(rid)
        assert row.access.grants == {BOB: "read"}, model


async def test_harness_writes_stay_admin_only(env):
    resp = await env.client.put("/v1/harnesses/hns_1", headers=_as(ALICE), json={"name": "x"})
    assert resp.status_code == 403
    assert (await env.client.put("/v1/harnesses/hns_1", headers=_as(ROOT), json={"name": "x"})).status_code in (200, 202)


async def test_the_harness_tools_check_read(env):
    tools = build_harness_toolset_provider(storage_provider=env.sp)

    def ctx(uid: str) -> ToolContext:
        return ToolContext(
            tool_call_id="c", session_id=None, workspace_id=None,
            initiated_by=PrincipalRef(type="user", id=uid, display=uid, role="admin" if uid == ROOT else "user", source="local", user_id=uid),
        )

    import json

    assert json.loads((await tools.call(tool_name="list", arguments={}, ctx=ctx(BOB))).output)["items"] == []
    got = await tools.call(tool_name="get", arguments={"id": "hns_1"}, ctx=ctx(BOB))
    assert got.is_error and json.loads(got.output)["type"] == "not-found"
    assert not (await tools.call(tool_name="get", arguments={"id": "hns_1"}, ctx=ctx(ALICE))).is_error
    assert len(json.loads((await tools.call(tool_name="list", arguments={}, ctx=ctx(ROOT))).output)["items"]) == 1


# --- sharing: the fan-out and the managed-row refusal -------------------------------------------------------------------------


async def test_sharing_an_installed_row_directly_is_refused_harness_managed(env):
    resp = await env.client.put(
        "/v1/agents/ag-1/access", headers=_as(ALICE), json={"owners": [ALICE], "grants": {BOB: "read"}, "rev": 0}
    )
    assert resp.status_code == 409, resp.text
    assert "harness_managed" in resp.text
    assert (await env.sp.get_storage(Agent).get("ag-1")).access.grants == {}


async def test_set_access_on_the_harness_fans_out_through_the_service(env):
    users = env.sp.get_storage(User)
    storage = env.sp.get_storage(Harness)
    seen: list[str] = []

    async def fan(row):
        seen.append(row.id)
        await apply_harness_access(env.sp, row)

    await set_access(
        storage, users, "hns_1", Person(ALICE, "user"), AccessUpdate(owners=[ALICE], grants={BOB: "full"}, rev=0),
        what="Harness 'hns_1'", after_write=fan,
    )
    assert seen == ["hns_1"]
    assert (await env.sp.get_storage(Graph).get("gr-1")).access.grants == {BOB: "full"}


async def test_the_fan_out_touches_only_the_access_of_rows_of_that_harness(env):
    await env.sp.get_storage(Agent).create(_agent("ag-other", harness_id="hns_2", access=Access(owners=[BOB])))
    await env.sp.get_storage(Agent).create(_agent("ag-own", harness_id=None, access=Access(owners=[BOB])))
    before = await env.sp.get_storage(Agent).get("ag-1")
    new = Access(owners=[ALICE], grants={BOB: "read"}, rev=1)
    await env.sp.get_storage(Harness).update((await env.sp.get_storage(Harness).get("hns_1")).model_copy(update={"access": new}))

    await apply_harness_access(env.sp, await env.sp.get_storage(Harness).get("hns_1"))

    after = await env.sp.get_storage(Agent).get("ag-1")
    assert after.access == new and after.model_copy(update={"access": before.access}) == before
    assert (await env.sp.get_storage(Agent).get("ag-other")).access.owners == [BOB]
    assert (await env.sp.get_storage(Agent).get("ag-own")).access.grants == {}


# --- a sync never writes access; export never carries it ----------------------------------------------------------------------


async def test_a_sync_write_keeps_a_revoke_that_landed_after_it_read(env):
    from primer.harness.service import _write_entity_with_body

    stale = await env.sp.get_storage(Agent).get("ag-1")  # the sync's read: shared with bob
    shared = Access(owners=[ALICE], grants={BOB: "read"}, rev=1)
    await env.sp.get_storage(Harness).update((await env.sp.get_storage(Harness).get("hns_1")).model_copy(update={"access": shared}))
    await apply_harness_access(env.sp, await env.sp.get_storage(Harness).get("hns_1"))
    # alice revokes: harness and rows move to a private rev 2
    private2 = Access(owners=[ALICE], rev=2)
    await env.sp.get_storage(Harness).update((await env.sp.get_storage(Harness).get("hns_1")).model_copy(update={"access": private2}))
    await apply_harness_access(env.sp, await env.sp.get_storage(Harness).get("hns_1"))

    entity = stale.model_copy(update={"description": "synced", "access": shared})
    await _write_entity_with_body(
        storage_provider=env.sp, storage=env.sp.get_storage(Agent), entity=entity, content=None, is_update=True,
    )

    row = await env.sp.get_storage(Agent).get("ag-1")
    assert row.description == "synced"
    assert row.access == private2


async def test_a_synced_create_takes_the_harness_access(env):
    from primer.harness.service import _write_entity_with_body

    await env.sp.get_storage(Harness).update(
        (await env.sp.get_storage(Harness).get("hns_1")).model_copy(update={"access": Access(owners=[ALICE], grants={BOB: "read"}, rev=1)})
    )
    await _write_entity_with_body(
        storage_provider=env.sp, storage=env.sp.get_storage(Agent), entity=_agent("ag-new", access=Access()), content=None, is_update=False,
    )
    assert (await env.sp.get_storage(Agent).get("ag-new")).access.grants == {BOB: "read"}


async def test_outbound_export_has_no_access(env):
    harness = Harness(
        id="hn-out", slug="out", name="Out", direction=HarnessDirection.OUTBOUND, git_url="https://github.com/x/y", created_at=NOW,
        tracked_entities=[TrackedEntity(kind="agent", source_id="ag-mine", template_name="mine")],
    )
    await env.sp.get_storage(Agent).create(_agent("ag-mine", harness_id=None, access=Access(owners=[BOB], grants={ALICE: "read"})))

    result = await build_outbound(harness, storage_provider=env.sp)

    text = next(f.rendered_text for f in result.files if f.template_path == "templates/mine.yaml")
    assert "\naccess:" not in "\n" + text and "owners" not in text


async def test_two_fan_outs_of_different_revs_leave_every_row_at_the_newer_one(env):
    import asyncio

    for i in range(6):
        await env.sp.get_storage(Agent).create(_agent(f"ag-x{i}"))
    harnesses = env.sp.get_storage(Harness)
    base = await harnesses.get("hns_1")
    rev5 = base.model_copy(update={"access": Access(owners=[ALICE], grants={BOB: "read"}, rev=5)})
    rev6 = base.model_copy(update={"access": Access(owners=[ALICE], rev=6)})  # bob's grant revoked

    await asyncio.gather(apply_harness_access(env.sp, rev5), apply_harness_access(env.sp, rev6))
    await apply_harness_access(env.sp, rev5)  # a straggler of the older fan-out lands last

    for model in (Agent, Graph, Collection):
        for row in (await env.sp.get_storage(model).list(OffsetPage(offset=0, length=50))).items:
            assert row.access.rev == 6 and row.access.grants == {}, (model, row.id)
