"""The cross-workspace lists filter in storage by the caller's readable workspaces (access-control P2 Task 5, Review Focus 4).

``GET /v1/sessions``, ``POST /v1/sessions/find``, ``GET /v1/sessions/recent``, ``GET /v1/yields/pending``, ``GET /v1/external_tool_calls`` and
``GET /v1/tool_approval/records`` answer a non-admin only the rows of workspaces they read (owner, grant or public), an admin everything. The filter
is part of the query, so a page's total counts what the person may read and the recent feed's scan window is filled from their own rows. The minimal
app of ``test_workspace_access_routes.py``: an ``X-Test-User`` actor, the real SQLite provider, the routers under test.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from pydantic import SecretStr

from primer.api.errors import register_error_handlers
from primer.api.routers import external_tools, sessions, tool_approval, workspaces
from primer.model.access import Access
from primer.model.external_tool import ExternalToolCall
from primer.model.provider import SqliteConfig
from primer.model.storage import FieldRef, OffsetPage, Op, Predicate, Value
from primer.model.tool_approval import ToolApprovalRecord
from primer.model.workspace import Workspace, WorkspaceRuntimeMeta
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.storage.sqlite import SqliteStorageProvider
from tests.api.test_workspace_access_routes import _ActorMiddleware, _as

NOW = datetime(2026, 1, 1, tzinfo=UTC)
ALICE, BOB, ADMIN = "alice", "bob", "admin"


def _app(sp: Any) -> FastAPI:
    app = FastAPI(title="cross-workspace lists (test)")
    register_error_handlers(app)
    app.state.storage_provider = sp
    app.state.provider_registry = object()  # _assert_app_state_initialized only checks it is set
    app.state.workspace_registry = object()
    app.add_middleware(_ActorMiddleware)
    app.include_router(sessions.top_session_router, prefix="/v1")
    app.include_router(workspaces.yields_pending_router, prefix="/v1")
    app.include_router(external_tools.external_tools_router, prefix="/v1")
    app.include_router(tool_approval.make_tool_approval_ops_router(), prefix="/v1")
    return app


async def _workspace(sp: Any, wid: str, *, owners: list[str], grants: dict[str, str] | None = None, public: bool = False) -> None:
    await sp.get_storage(Workspace).create(Workspace(
        id=wid, name=wid, template_id="tpl", provider_id="local-1", created_at=NOW,
        runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959", token=SecretStr("t")),
        access=Access(owners=owners, grants=grants or {}, public=public),
    ))


async def _session(sp: Any, sid: str, wid: str, *, age: int = 0, parked: bool = False) -> None:
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id=sid, workspace_id=wid, binding=AgentSessionBinding(agent_id="ag"), status=SessionStatus.WAITING,
        created_at=NOW + timedelta(seconds=age), parked_status="parked" if parked else None, parked_at=NOW if parked else None,
        parked_state={"yielded": {"tool_name": "ask_user", "resume_metadata": {}}} if parked else None,
    ))


async def _external(sp: Any, tcid: str, sid: str, wid: str | None) -> None:
    await sp.get_storage(ExternalToolCall).create(ExternalToolCall(
        session_id=sid, workspace_id=wid, tool_call_id=tcid, tool_name="t", created_at=NOW,
    ))


async def _record(sp: Any, key: str, sid: str | None, wid: str | None) -> None:
    await sp.get_storage(ToolApprovalRecord).create(ToolApprovalRecord(
        tool_name="t", session_id=sid, workspace_id=wid, gate_event_key=key, decided_at=NOW, decision="approved",
    ))


@pytest.fixture
async def world(tmp_path: Path):
    """ws-a owned by alice, ws-b owned by bob, ws-c public (owned by bob), ws-d owned by bob and shared read with alice; one of each row in each."""
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "lists.sqlite"))
    await sp.initialize()
    await _workspace(sp, "ws-a", owners=[ALICE])
    await _workspace(sp, "ws-b", owners=[BOB])
    await _workspace(sp, "ws-c", owners=[BOB], public=True)
    await _workspace(sp, "ws-d", owners=[BOB], grants={ALICE: "read"})
    for wid in ("ws-a", "ws-b", "ws-c", "ws-d"):
        suffix = wid[-1]
        await _session(sp, f"s-{suffix}", wid, parked=True)
        await _external(sp, f"tc-{suffix}", f"s-{suffix}", wid)
        await _record(sp, f"k-{suffix}", f"s-{suffix}", wid)
    await _external(sp, "tc-chat", "s-none", None)
    async with httpx.AsyncClient(transport=ASGITransport(app=_app(sp)), base_url="http://test") as client:
        yield sp, client
    await sp.aclose()


# alice reads ws-a (owner), ws-c (public) and ws-d (read grant); bob owns ws-b, ws-c and ws-d.
ALICE_SEES = {"a", "c", "d"}
BOB_SEES = {"b", "c", "d"}
ALL = {"a", "b", "c", "d"}


def _find(predicate: Predicate) -> dict:
    return {"predicate": predicate.model_dump(mode="json"), "page": OffsetPage(offset=0, length=50).model_dump(mode="json")}


def _ids(rows: list[dict], key: str, prefix: str) -> set[str]:
    return {r[key].removeprefix(prefix) for r in rows}


@pytest.mark.parametrize(("who", "role", "expected"), [(ALICE, "user", ALICE_SEES), (BOB, "user", BOB_SEES), (ADMIN, "admin", ALL)])
async def test_the_session_list_serves_only_readable_workspaces(world, who, role, expected):
    _, client = world
    body = (await client.get("/v1/sessions", headers=_as(who, role))).json()
    assert _ids(body["items"], "id", "s-") == expected
    assert body["total"] == len(expected), "the total counts what the person may read"
    scoped = (await client.get("/v1/sessions", params={"status": "waiting"}, headers=_as(who, role))).json()
    assert _ids(scoped["items"], "id", "s-") == expected


async def test_a_workspace_filter_cannot_reach_an_unreadable_workspace(world):
    _, client = world
    body = (await client.get("/v1/sessions", params={"workspace_id": "ws-b"}, headers=_as(ALICE))).json()
    assert body["items"] == [] and body["total"] == 0


@pytest.mark.parametrize(("who", "role", "expected"), [(ALICE, "user", ALICE_SEES), (ADMIN, "admin", ALL)])
async def test_session_find_is_anded_with_the_readable_workspaces(world, who, role, expected):
    _, client = world
    everything = _find(Predicate(left=FieldRef(name="status"), op=Op.EQ, right=Value(value="waiting")))
    body = (await client.post("/v1/sessions/find", json=everything, headers=_as(who, role))).json()
    assert _ids(body["items"], "id", "s-") == expected
    widen = _find(Predicate(left=FieldRef(name="workspace_id"), op=Op.EQ, right=Value(value="ws-b")))
    narrow = (await client.post("/v1/sessions/find", json=widen, headers=_as(who, role))).json()
    assert _ids(narrow["items"], "id", "s-") == ({"b"} if who == ADMIN else set())


async def test_a_person_who_reads_nothing_gets_an_empty_page(world):
    """A restricted user reads no workspace (a full user without a grant still reads the public ones)."""
    _, client = world
    for path in ("/v1/sessions", "/v1/external_tool_calls", "/v1/tool_approval/records"):
        body = (await client.get(path, headers=_as("stranger", "restricted"))).json()
        assert body["items"] == [] and body["total"] == 0, path
    assert (await client.get("/v1/sessions/recent", headers=_as("stranger", "restricted"))).json() == {"items": []}
    assert (await client.get("/v1/yields/pending", headers=_as("stranger", "restricted"))).json() == {"items": [], "total": 0}


@pytest.mark.parametrize(("who", "role", "expected"), [(ALICE, "user", ALICE_SEES), (ADMIN, "admin", ALL)])
async def test_recent_serves_only_readable_workspaces(world, who, role, expected):
    _, client = world
    body = (await client.get("/v1/sessions/recent", headers=_as(who, role))).json()
    assert _ids(body["items"], "session_id", "s-") == expected


async def test_recent_is_filtered_before_the_window(world):
    """Review Focus 4: alice's session is older than 200 newer sessions of bob's workspace; the scan window must be filled from alice's own rows."""
    sp, client = world
    for i in range(210):
        await _session(sp, f"bob-{i}", "ws-b", age=1000 + i)
    body = (await client.get("/v1/sessions/recent", params={"limit": 100}, headers=_as(ALICE))).json()
    assert _ids(body["items"], "session_id", "s-") == ALICE_SEES
    assert not any(r["session_id"].startswith("bob-") for r in body["items"])


@pytest.mark.parametrize(("who", "role", "expected"), [(ALICE, "user", ALICE_SEES), (ADMIN, "admin", ALL)])
async def test_pending_attention_serves_only_readable_workspaces(world, who, role, expected):
    _, client = world
    body = (await client.get("/v1/yields/pending", headers=_as(who, role))).json()
    assert _ids(body["items"], "session_id", "s-") == expected
    assert body["total"] == len(expected)


@pytest.mark.parametrize(("who", "role", "expected"), [(ALICE, "user", ALICE_SEES), (ADMIN, "admin", ALL | {"chat"})])
async def test_external_tool_calls_serve_only_readable_workspaces(world, who, role, expected):
    _, client = world
    body = (await client.get("/v1/external_tool_calls", headers=_as(who, role))).json()
    assert _ids(body["items"], "tool_call_id", "tc-") == expected
    assert body["total"] == len(expected)


@pytest.mark.parametrize(("who", "role", "expected"), [(ALICE, "user", ALICE_SEES), (ADMIN, "admin", ALL)])
async def test_approval_records_serve_only_readable_workspaces(world, who, role, expected):
    _, client = world
    for params in ({}, {"status": "approved"}):
        body = (await client.get("/v1/tool_approval/records", params=params, headers=_as(who, role))).json()
        assert _ids(body["items"], "gate_event_key", "k-") == expected, params
        assert body["total"] == len(expected), params
    one = (await client.get("/v1/tool_approval/records", params={"gate_event_key": "k-b"}, headers=_as(who, role))).json()
    assert [r["gate_event_key"] for r in one["items"]] == (["k-b"] if who == ADMIN else [])


async def test_a_revoke_removes_the_rows(world):
    sp, client = world
    before = (await client.get("/v1/sessions", headers=_as(ALICE))).json()
    assert "s-d" in {r["id"] for r in before["items"]}
    row = await sp.get_storage(Workspace).get("ws-d")
    await sp.get_storage(Workspace).update(row.model_copy(update={"access": Access(owners=[BOB])}))
    for path, key, prefix in (
        ("/v1/sessions", "id", "s-"), ("/v1/yields/pending", "session_id", "s-"), ("/v1/sessions/recent", "session_id", "s-"),
        ("/v1/external_tool_calls", "tool_call_id", "tc-"), ("/v1/tool_approval/records", "gate_event_key", "k-"),
    ):
        body = (await client.get(path, headers=_as(ALICE))).json()
        assert _ids(body["items"], key, prefix) == {"a", "c"}, path
