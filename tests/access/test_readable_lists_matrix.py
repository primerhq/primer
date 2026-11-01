"""Every list door that filters by the readable parents answers the same rows at every size (perf plan Task 4 stage B, finding H1).

The doors: ``GET /v1/workspaces``, ``POST /v1/workspaces/find``, ``GET /v1/sessions``, ``POST /v1/sessions/find``, ``GET /v1/sessions/recent``,
``GET /v1/yields/pending``, ``GET /v1/external_tool_calls``, ``GET /v1/tool_approval/records``, ``GET /v1/documents``, and the ``find_sessions``,
``find_documents`` and ``list_graph_threads`` tools. Each runs against real SQLite with 0, 1, 199, 200, 201 and 450 parents a person can see (owned,
granted read, granted full, public, jointly owned) plus rows they cannot. The expected answer is an ORACLE: every parent judged by ``level_of``,
then the admin's own ordered answer (the unfiltered path) kept to the readable parents, or the route's explicit order rebuilt in Python where the
route caps its answer. A person who reads nothing keeps the exact empty answer each door gives today (some doors answer it without a query, in a
shape of their own). These tests pass on the code before stage B and must pass unchanged after it.
"""
from __future__ import annotations

import json
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from primer.access.person import Person
from primer.access.workspaces import readable_workspace_ids, scope_to_readable_workspaces
from primer.agent.inner_dispatch import dispatch_inner_tool
from primer.api.errors import register_error_handlers
from primer.api.routers import external_tools, knowledge, sessions, tool_approval, workspaces
from primer.model.access import Access, AccessLevel
from primer.model.collection import Collection, Document
from primer.model.external_tool import ExternalToolCall
from primer.model.graph import Graph, GraphThread
from primer.model.principal import PrincipalRef
from primer.model.provider import SqliteConfig
from primer.model.storage import FieldRef, OffsetPage, Op, Predicate, Value
from primer.model.tool_approval import ToolApprovalRecord
from primer.model.workspace import Workspace
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.model.yield_ import ToolContext
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset._collection_access import readable_documents
from primer.toolset.system import build_system_toolset
from primer.toolset.workspaces import WORKSPACES_TOOLSET_ID, build_workspaces_toolset
from tests.access.test_readable_ids_matrix import HIDDEN, ME, NOW, OTHER, PEOPLE, SIZES, _access_for, _all, _oracle, _ws
from tests.api.test_workspace_access_routes import _ActorMiddleware, _as

#: The people the doors are asked as: ``(user id, role)``.
WHO = {
    "me": (ME, "user"),
    "other": (OTHER, "user"),
    "stranger": ("u-stranger", "user"),
    "restricted": (ME, "restricted"),
    "admin": ("u-admin", "admin"),
}
PAGE = {"kind", "offset", "length", "total", "items"}
#: ``empty_page``: the page the session doors answer a person who reads no workspace, without a query (and without ``kind``).
EMPTY_PAGE = {"offset", "length", "total", "items"}


def _app(sp: Any) -> FastAPI:
    app = FastAPI(title="readable lists matrix (test)")
    register_error_handlers(app)
    app.state.storage_provider = sp
    app.state.provider_registry = object()
    app.state.workspace_registry = object()
    app.state.claim_engine = None
    app.add_middleware(_ActorMiddleware)
    app.include_router(workspaces.workspace_router, prefix="/v1")
    app.include_router(workspaces.yields_pending_router, prefix="/v1")
    app.include_router(sessions.top_session_router, prefix="/v1")
    app.include_router(external_tools.external_tools_router, prefix="/v1")
    app.include_router(tool_approval.make_tool_approval_ops_router(), prefix="/v1")
    app.include_router(knowledge.document_router, prefix="/v1")
    return app


async def _seed(sp: Any, visible: int) -> None:
    """``visible`` parents of each kind ME can see, ``HIDDEN`` more ME cannot; one child row per parent (one session, tool call and approval
    record per workspace, one document per collection, one run per graph). Sessions carry distinct times so every order is total."""
    async with sp.transaction() as conn:
        parents = [(f"v{i:04d}", _access_for(i)) for i in range(visible)]
        parents += [(f"h{i:03d}", Access(owners=[OTHER], grants={"u-third": "read"})) for i in range(HIDDEN)]
        for n, (suffix, access) in enumerate(parents):
            wid, kid, gid = f"ws-{suffix}", f"kb-{suffix}", f"g-{suffix}"
            at = NOW + timedelta(seconds=(n * 7919) % 1009)  # times out of id order, so an order by time is not an order by id
            await sp.get_storage(Workspace).create(_ws(wid, access), conn=conn)
            await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
                id=f"s-{wid}", workspace_id=wid, binding=AgentSessionBinding(agent_id="ag"), status=SessionStatus.WAITING, created_at=at,
                parked_status="parked", parked_at=at, parked_state={"yielded": {"tool_name": "ask_user", "resume_metadata": {}}},
            ), conn=conn)
            await sp.get_storage(ExternalToolCall).create(ExternalToolCall(
                session_id=f"s-{wid}", workspace_id=wid, tool_call_id=f"tc-{wid}", tool_name="t", created_at=NOW,
            ), conn=conn)
            await sp.get_storage(ToolApprovalRecord).create(ToolApprovalRecord(
                tool_name="t", session_id=f"s-{wid}", workspace_id=wid, gate_event_key=f"k-{wid}", decided_at=at, decision="approved",
            ), conn=conn)
            await sp.get_storage(Collection).create(Collection(id=kid, description="d", access=access), conn=conn)
            await sp.get_storage(Document).create(
                Document(id=f"d-{kid}", collection_id=kid, slug=f"s-{suffix}", path=f"p-{suffix}", title="t"), conn=conn,
            )
            await sp.get_storage(Graph).create(Graph(id=gid, description="g", nodes=[], edges=[], access=access), conn=conn)
            await sp.get_storage(GraphThread).create(
                GraphThread(id=f"gt-{gid}", graph_id=gid, title=gid, created_at=NOW, last_activity_at=NOW), conn=conn,
            )
        # A chat call has no workspace: only an admin lists it.
        await sp.get_storage(ExternalToolCall).create(ExternalToolCall(
            session_id="s-none", workspace_id=None, tool_call_id="tc-chat", tool_name="t", created_at=NOW,
        ), conn=conn)


async def _sqlite(tmp_path, visible: int) -> SqliteStorageProvider:
    sp = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "lists.sqlite")))
    await sp.initialize()
    await _seed(sp, visible)
    return sp


# ---- reading every page of a door -----------------------------------------------------------------------------------------------------------


async def _get_pages(client, path: str, who, *, size_param: str = "limit") -> tuple[list[dict], list[dict]]:
    """Every item of an offset-paged GET door, and every page body."""
    items, bodies, offset = [], [], 0
    while True:
        body = (await client.get(path, params={size_param: 200, "offset": offset}, headers=_as(*who))).json()
        bodies.append(body)
        items.extend(body["items"])
        if len(body["items"]) < 200:
            return items, bodies
        offset += 200


async def _find_pages(client, path: str, who, predicate: Predicate) -> tuple[list[dict], list[dict]]:
    items, bodies, offset = [], [], 0
    while True:
        page = OffsetPage(offset=offset, length=200).model_dump(mode="json")
        body = (await client.post(path, json={"predicate": predicate.model_dump(mode="json"), "page": page}, headers=_as(*who))).json()
        bodies.append(body)
        items.extend(body["items"])
        if len(body["items"]) < 200:
            return items, bodies
        offset += 200


def _ref(who) -> PrincipalRef:
    uid, role = who
    return PrincipalRef(type="user", id=uid, display=uid, role=role, source="local", user_id=uid)


async def _tool_pages(toolset, tool: str, who) -> list[dict]:
    items, offset = [], 0
    ctx = ToolContext(tool_call_id="tc-1", session_id=None, workspace_id=None, initiated_by=_ref(who))
    while True:
        res = await toolset.call(tool_name=tool, arguments={"limit": 200, "offset": offset}, ctx=ctx)
        assert not res.is_error, res.output
        body = json.loads(res.output)
        items.extend(body["items"])
        if len(body["items"]) < 200:
            assert body["total"] == len(items)
            return items
        offset += 200


async def _find_sessions_tool(sp, who) -> dict:
    toolset = build_workspaces_toolset(storage_provider=sp, workspace_registry=MagicMock())
    ctx = ToolContext(tool_call_id="tc-1", session_id=None, workspace_id=None, initiated_by=_ref(who))
    res = await dispatch_inner_tool(provider=toolset, toolset_id=WORKSPACES_TOOLSET_ID, tool_name="find_sessions", arguments={"limit": 200}, ctx=ctx)
    assert not res.is_error, res.output
    return json.loads(res.output)


# ---- the doors, one person at a time --------------------------------------------------------------------------------------------------------


def _readable(rows, who) -> set[str]:
    uid, role = who
    ids = _oracle(rows, Person(uid, role), AccessLevel.READ)
    return {row.id for row in rows} if ids is None else set(ids)


async def _answers(sp, client, system, who) -> dict[str, Any]:
    """What every door answers ``who``: ordered ids, and the body shape and totals the doors report."""
    out: dict[str, Any] = {}
    items, bodies = await _get_pages(client, "/v1/workspaces", who)
    out["workspaces"] = [r["id"] for r in items]
    out["workspaces.shape"] = (set(bodies[0]), bodies[-1]["total"])
    status_waiting = Predicate(left=FieldRef(name="status"), op=Op.EQ, right=Value(value="waiting"))
    items, bodies = await _find_pages(client, "/v1/workspaces/find", who, Predicate(left=FieldRef(name="template_id"), op=Op.EQ, right=Value(value="t")))
    out["workspaces/find"] = [r["id"] for r in items]
    out["workspaces/find.shape"] = (set(bodies[0]), bodies[-1]["total"])
    items, bodies = await _get_pages(client, "/v1/sessions", who)
    out["sessions"] = [r["id"] for r in items]
    out["sessions.shape"] = (set(bodies[0]), bodies[-1]["total"])
    items, bodies = await _find_pages(client, "/v1/sessions/find", who, status_waiting)
    out["sessions/find"] = [r["id"] for r in items]
    out["sessions/find.shape"] = (set(bodies[0]), bodies[-1]["total"])
    body = (await client.get("/v1/sessions/recent", params={"limit": 100}, headers=_as(*who))).json()
    out["recent"] = [r["session_id"] for r in body["items"]]
    out["recent.shape"] = set(body)
    body = (await client.get("/v1/yields/pending", params={"limit": 500}, headers=_as(*who))).json()
    out["pending"] = [r["session_id"] for r in body["items"]]
    out["pending.shape"] = (set(body), body["total"])
    items, bodies = await _get_pages(client, "/v1/external_tool_calls", who)
    out["external"] = [r["tool_call_id"] for r in items]
    out["external.shape"] = (set(bodies[0]), bodies[-1]["total"])
    items, bodies = await _get_pages(client, "/v1/tool_approval/records", who, size_param="length")
    out["records"] = [r["gate_event_key"] for r in items]
    out["records.shape"] = (set(bodies[0]), bodies[-1]["total"])
    items, bodies = await _get_pages(client, "/v1/documents", who)
    out["documents"] = [r["id"] for r in items]
    out["documents.shape"] = (set(bodies[0]), bodies[-1]["total"])
    if who[1] != "restricted":  # the inner dispatch refuses find_sessions to a restricted person by role, before any list
        body = await _find_sessions_tool(sp, who)
        out["find_sessions"] = ([r["id"] for r in body["items"]], body["total"])
    out["find_documents"] = [r["id"] for r in await _tool_pages(system, "find_documents", who)]
    out["list_graph_threads"] = [r["id"] for r in await _tool_pages(system, "list_graph_threads", who)]
    return out


def _expected(admin: dict[str, Any], who, *, workspaces_rows, collection_rows, graph_rows, session_rows) -> dict[str, Any]:
    """The oracle: the admin's ordered answers kept to the parents ``who`` reads; the capped doors rebuilt from the rows."""
    ws_ok, kb_ok, g_ok = _readable(workspaces_rows, who), _readable(collection_rows, who), _readable(graph_rows, who)
    nothing = not ws_ok
    newest = [s.id for s in sorted(session_rows, key=lambda s: (s.created_at, s.id), reverse=True) if s.workspace_id in ws_ok]

    def keep(key: str, parent_of) -> list[str]:
        return list(admin[key]) if who[1] == "admin" else [x for x in admin[key] if parent_of(x) in ws_ok]

    out: dict[str, Any] = {}
    out["workspaces"] = keep("workspaces", lambda x: x)
    out["workspaces/find"] = keep("workspaces/find", lambda x: x)
    out["sessions"] = keep("sessions", lambda x: x.removeprefix("s-"))
    out["sessions/find"] = keep("sessions/find", lambda x: x.removeprefix("s-"))
    for key in ("workspaces", "workspaces/find"):
        out[f"{key}.shape"] = (PAGE, len(out[key]))
    for key in ("sessions", "sessions/find"):
        out[f"{key}.shape"] = (EMPTY_PAGE if nothing else PAGE, len(out[key]))
    out["recent"] = newest[:100]
    out["recent.shape"] = {"items"}
    out["pending"] = keep("pending", lambda x: x.removeprefix("s-"))
    out["pending.shape"] = ({"items", "total"}, len(out["pending"]))
    out["external"] = keep("external", lambda x: x.removeprefix("tc-"))
    # A person who reads no workspace gets the door's own empty answer, without its paging keys.
    out["external.shape"] = ({"items", "total"} if nothing else {"items", "total", "offset", "limit"}, len(out["external"]))
    out["records"] = keep("records", lambda x: x.removeprefix("k-"))
    out["records.shape"] = (PAGE, len(out["records"]))
    out["documents"] = [x for x in admin["documents"] if x.removeprefix("d-") in kb_ok]
    out["documents.shape"] = (PAGE, len(out["documents"]))
    if who[1] != "restricted":
        out["find_sessions"] = (newest[:200], len(newest))
    out["find_documents"] = [x for x in admin["find_documents"] if x.removeprefix("d-") in kb_ok]
    out["list_graph_threads"] = [x for x in admin["list_graph_threads"] if x.removeprefix("gt-") in g_ok]
    return out


async def _rows(sp) -> dict[str, list]:
    return {
        "workspaces_rows": await _all(sp.get_storage(Workspace)),
        "collection_rows": await _all(sp.get_storage(Collection)),
        "graph_rows": await _all(sp.get_storage(Graph)),
        "session_rows": await _all(sp.get_storage(WorkspaceSession)),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("visible", SIZES)
async def test_every_list_door_answers_the_readable_rows_at_every_size(visible, tmp_path):
    sp = await _sqlite(tmp_path, visible)
    rows = await _rows(sp)
    system = build_system_toolset(storage_provider=sp, provider_registry=MagicMock())
    async with httpx.AsyncClient(transport=ASGITransport(app=_app(sp)), base_url="http://test") as client:
        admin = await _answers(sp, client, system, WHO["admin"])
        assert len(admin["workspaces"]) == visible + HIDDEN and len(admin["external"]) == visible + HIDDEN + 1
        for name, who in WHO.items():
            got = await _answers(sp, client, system, who)
            want = _expected(admin, who, **rows)
            for key in want:
                assert got[key] == want[key], (name, key, visible)
    assert len(_readable(rows["workspaces_rows"], WHO["me"])) == visible
    # The need=FULL id set stays the stage-A walk: owned, full grants and joint ownership; never read grants or public rows.
    full = await readable_workspace_ids(sp, PEOPLE["me"], AccessLevel.FULL)
    assert sorted(full) == _oracle(rows["workspaces_rows"], PEOPLE["me"], AccessLevel.FULL)
    await sp.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("visible", SIZES)
async def test_the_scope_helpers_match_the_oracle_for_every_person(visible, tmp_path):
    """``scope_to_readable_workspaces`` and ``readable_documents`` for every kind of person, including the ones no route header makes (a full user
    without a user id, the auth-disabled system principal)."""
    sp = await _sqlite(tmp_path, visible)
    rows = await _rows(sp)
    session_store, document_store = sp.get_storage(WorkspaceSession), sp.get_storage(Document)
    all_sessions, all_documents = rows["session_rows"], await _all(document_store)
    for name, person in PEOPLE.items():
        readable = _oracle(rows["workspaces_rows"], person, AccessLevel.READ)
        nothing, scoped = await scope_to_readable_workspaces(sp, person)
        if readable is None:
            assert (nothing, scoped) == (False, None), name
        else:
            assert nothing is (not readable), name
            if readable:
                assert sorted(s.id for s in await _found(session_store, scoped)) == sorted(s.id for s in all_sessions if s.workspace_id in set(readable)), name
        kb_readable = _oracle(rows["collection_rows"], person, AccessLevel.READ)
        predicate = await readable_documents(sp.get_storage(Collection), person)
        if kb_readable is None:
            assert predicate is None, name
        else:
            want = sorted(d.id for d in all_documents if d.collection_id in set(kb_readable))
            assert sorted(d.id for d in await _found(document_store, predicate)) == want, name
    await sp.aclose()


async def _found(store, predicate) -> list:
    rows, offset = [], 0
    while True:
        page = await store.find(predicate, OffsetPage(offset=offset, length=200))
        rows.extend(page.items)
        if len(page.items) < 200:
            return rows
        offset += 200


@pytest.mark.asyncio
async def test_a_revoked_grant_and_a_made_private_parent_leave_every_door_on_the_next_request(tmp_path):
    sp = await _sqlite(tmp_path, 12)
    system = build_system_toolset(storage_provider=sp, provider_registry=MagicMock())
    me = WHO["me"]
    # v0001 is a read grant, v0002 a full grant, v0003 public: take all three away from ME, on every kind of parent.
    gone = {"v0001", "v0002", "v0003"}
    async with httpx.AsyncClient(transport=ASGITransport(app=_app(sp)), base_url="http://test") as client:
        before = await _answers(sp, client, system, me)
        assert all(f"s-ws-{s}" in before["sessions"] for s in gone)
        for model, prefix in ((Workspace, "ws-"), (Collection, "kb-"), (Graph, "g-")):
            store = sp.get_storage(model)
            for suffix in gone:
                row = await store.get(f"{prefix}{suffix}")
                await store.update(row.model_copy(update={"access": Access(owners=[OTHER])}))
        after = await _answers(sp, client, system, me)
        admin = await _answers(sp, client, system, WHO["admin"])
        want = _expected(admin, me, **await _rows(sp))
        for key in want:
            assert after[key] == want[key], key
    for key in ("workspaces", "sessions", "pending", "external", "records", "documents", "find_documents", "list_graph_threads"):
        left = {x for x in before[key] for s in gone if x.endswith(s)}
        assert left and not left & set(after[key]), key
    await sp.aclose()
