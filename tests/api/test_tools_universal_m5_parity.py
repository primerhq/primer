"""M5 representative REST / in-platform / MCP parity (matrix section 7).

The ten representatives span every mount_auth class, every rbac class, all
three mapping states, and the distinct operation shapes (simple CRUD read,
nested body, one-to-many, approval-gated, endpoint-enforced visibility).
For each: the REST status/visibility, the ``GET /v1/tools/catalogue``
visibility, and the in-process MCP surface (``authorized_catalogue`` +
``list_tools`` / ``call_tool`` through ``ExposureDeps``), with the RFC 7807
problem-envelope identity and the ``call_tool_rejection`` audit channel on
refusals.

Observed deviations from the matrix wording, asserted here as actual
behavior and reported to the lead:

* The catalogue's built-in side is NOT role-filtered (``tools.py`` filters
  only the user-defined toolset rows), so a user sees admin-only built-in
  tool ids in the catalogue while the MCP side hides them (row 9).
* Row 9's matrix label "(curated)" is stale: ``_create_v1_services_post``
  is generated ``services__create``.
* Row 7's malformed-path RFC 7807 body identity is unreachable: the
  generated argument schema is stricter than the REST body (jsonschema
  gates before dispatch), so parity is asserted on the success-path body
  identity plus the same-field rejection.
* Row 4: a different user's ``call_tool`` is not refused; it answers with
  that user's OWN (empty) token list. The owner's token ids leak to
  neither surface, and the anonymous caller is refused on both (REST 401
  ``auth_required``; MCP closed door).
* The MCP closed door: an anonymous actor (no session at all) never passes
  the role rank, so it sees zero tools regardless of exposure state.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from pydantic import SecretStr

import pytest
from httpx import ASGITransport, AsyncClient

from primer.mcp.exposure import ExposureDeps, update_exposure
from primer.mcp.meta_tools import call_tool, call_tool_rejection, list_tools
from primer.mcp.server import (
    current_actor,
    current_api_token_scopes,
    current_principal,
)
from primer.model.access import Access
from primer.model.principal import Principal
from primer.model.workspace import Workspace, WorkspaceRuntimeMeta
from primer.model.tool_approval import ToolApprovalPolicy
from primer.bootstrap.seed import default_policy_id

MANIFEST_PATH = Path("scripts/data/tools_universal_m0_manifest.json")


def _actor(role: str, user_id: str) -> Principal:
    return Principal(
        type="user", id=user_id, user_id=user_id, display="U", role=role, source="local"
    )


def _set_context(role: str = "user", user_id: str = ""):
    """Bind the MCP request context (real user id: the trusted in-process
    dispatch re-reads the user row, so a fake id would 401 inside)."""
    t1 = current_actor.set(None if not user_id else _actor(role=role, user_id=user_id))
    t2 = current_api_token_scopes.set(None if not user_id else ["mcp"])
    t3 = current_principal.set(None if not user_id else f"{user_id}@example.com")
    return (
        (current_actor, t1),
        (current_api_token_scopes, t2),
        (current_principal, t3),
    )


def _reset(tokens) -> None:
    for var, token in tokens:
        var.reset(token)


async def _catalogue_ids(deps: ExposureDeps) -> set[str]:
    """Walk every page of the caller's MCP catalogue (default page cap 100)."""
    ids: set[str] = set()
    cursor: str | None = None
    while True:
        args: dict[str, Any] = {"limit": 100}
        if cursor:
            args["cursor"] = cursor
        page = await list_tools(deps, args)
        ids |= {item["scoped_id"] for item in page["items"]}
        cursor = page.get("next_cursor")
        if not cursor:
            return ids


@pytest.fixture
async def user2(app, client) -> AsyncClient:
    """A non-admin user (role ``user``): the parity 'other caller'."""
    r = await client.post(
        "/v1/admin/users",
        json={"username": "parity-bob", "password": "bobpassword", "role": "user"},
    )
    assert r.status_code in (200, 201), r.text
    c = AsyncClient(transport=ASGITransport(app=app), base_url="http://t")
    login = await c.post(
        "/v1/auth/login", json={"username": "parity-bob", "password": "bobpassword"}
    )
    assert login.status_code == 200, login.text
    yield c
    await c.aclose()


@pytest.fixture
async def mcp(app) -> ExposureDeps:
    """In-process MCP deps with the exposure switch ON (the safe default is OFF)."""
    deps = ExposureDeps(
        storage_provider=app.state.storage_provider,
        provider_registry=app.state.provider_registry,
        approval_resolver=app.state.approval_resolver,
    )
    await update_exposure(
        enabled=True, mode="rbac", allowed_tools=None, updated_by="m5-parity", deps=deps
    )
    return deps


async def _user_id(client: AsyncClient) -> str:
    return (await client.get("/v1/auth/status")).json()["principal"]["id"]


def _manifest_row(operation_id: str) -> dict:
    return next(
        row
        for row in json.loads(MANIFEST_PATH.read_text())["rows"]
        if row["operation_id"] == operation_id
    )


# --- row 1: public (generated) -------------------------------------------------------------


async def test_row1_health_public_visible_on_all_surfaces(client, raw_client, mcp) -> None:
    r = await raw_client.get("/v1/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    # The catalogue is an authenticated surface: anonymous is refused, every
    # signed-in caller sees the tool.
    assert (await raw_client.get("/v1/tools/catalogue")).status_code == 401
    r = await client.get("/v1/tools/catalogue")
    assert "health__health" in {item["id"] for item in r.json()["items"]}
    # MCP: a signed-in caller sees and can call it; the anonymous actor is
    # closed the whole door (no tool, not even the public one).
    owner_id = await _user_id(client)
    toks = _set_context(role="admin", user_id=owner_id)
    try:
        assert "health__health" in await _catalogue_ids(mcp)
        out = await call_tool(mcp, {"scoped_id": "health__health", "arguments": {}})
        assert out["ok"] is True
        assert json.loads(out["output"])["status"] == "ok"
        assert call_tool_rejection.get() is None
    finally:
        _reset(toks)
    toks = _set_context()
    try:
        assert "health__health" not in await _catalogue_ids(mcp)
        out = await call_tool(mcp, {"scoped_id": "health__health", "arguments": {}})
        assert out == {"ok": False, "error": {"code": "tool_unavailable", "message": "Tool is unavailable."}}
        assert call_tool_rejection.get() is None
    finally:
        _reset(toks)


# --- row 2: human-auth (generated) ---------------------------------------------------------


async def test_row2_auth_status_preauth_visible_no_approval(client, raw_client, mcp) -> None:
    r = await raw_client.get("/v1/auth/status")
    assert r.status_code == 200
    body = r.json()
    assert body["has_user"] is True and body["authenticated"] is False
    r = await client.get("/v1/tools/catalogue")
    assert "auth__auth_status" in {item["id"] for item in r.json()["items"]}
    owner_id = await _user_id(client)
    toks = _set_context(role="admin", user_id=owner_id)
    try:
        out = await call_tool(mcp, {"scoped_id": "auth__auth_status", "arguments": {}})
        assert out["ok"] is True, out
        # The identity is projected from the MCP actor, not a session cookie:
        # pre-auth the REST body said authenticated False, the call says True.
        assert json.loads(out["output"])["authenticated"] is True
        assert call_tool_rejection.get() is None
    finally:
        _reset(toks)


# --- row 3: human-auth (skip) --------------------------------------------------------------


async def test_row3_login_is_the_frozen_skip_absent_from_tool_surfaces(raw_client, client, user2, mcp) -> None:
    r = await raw_client.post(
        "/v1/auth/login", json={"username": "parity-bob", "password": "wrong"}
    )
    assert r.status_code == 401
    r = await raw_client.post(
        "/v1/auth/login", json={"username": "parity-bob", "password": "bobpassword"}
    )
    assert r.status_code == 200
    assert r.json()["username"] == "parity-bob"
    # The reviewed skip contract: login is a human sign-in surface, never a
    # tool, so no catalogue item and no MCP entry may name it.
    row = _manifest_row("login_v1_auth_login_post")
    assert row["state"] == "skip" and row["skip_code"] == "human_sign_in"
    r = await client.get("/v1/tools/catalogue")
    assert not any(item["id"].endswith("__login") for item in r.json()["items"])
    owner_id = await _user_id(client)
    toks = _set_context(role="admin", user_id=owner_id)
    try:
        assert "auth__login" not in await _catalogue_ids(mcp)
    finally:
        _reset(toks)


# --- row 4: authenticated-account (generated) ---------------------------------------------


async def test_row4_auth_tokens_never_leak_across_users(client, raw_client, user2, mcp) -> None:
    r = await raw_client.get("/v1/auth/tokens")
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/problem+json")
    problem = r.json()
    assert problem["type"] == "/errors/authentication-failed"
    assert problem["extensions"]["error"] == "auth_required"
    # The owner mints a token through the cookie session (bearer is refused
    # by the router-internal check, so a REST caller must be the owner).
    r = await client.post(
        "/v1/auth/tokens", json={"name": "m5-parity-token", "scopes": ["mcp"]}
    )
    assert r.status_code == 201
    token_id = r.json()["id"]
    r = await client.get("/v1/auth/tokens")
    assert [item["id"] for item in r.json()["items"]] == [token_id]
    # A different user sees their own (empty) list on both surfaces: the
    # owner's token ids leak to neither.
    r = await user2.get("/v1/auth/tokens")
    assert r.status_code == 200 and r.json()["items"] == []
    bob_id = await _user_id(user2)
    toks = _set_context(role="user", user_id=bob_id)
    try:
        out = await call_tool(mcp, {"scoped_id": "auth__list_api_tokens", "arguments": {}})
        assert out["ok"] is True, out
        assert json.loads(out["output"]) == {"items": []}
        assert call_tool_rejection.get() is None
    finally:
        _reset(toks)
    # Anonymous: refused on the MCP side too (the closed door, not the tool).
    toks = _set_context()
    try:
        out = await call_tool(mcp, {"scoped_id": "auth__list_api_tokens", "arguments": {}})
        assert out == {"ok": False, "error": {"code": "tool_unavailable", "message": "Tool is unavailable."}}
    finally:
        _reset(toks)


# --- row 5: global-settings-read (curated) -------------------------------------------------


async def test_row5_settings_read_route_and_mcp_role_floor(client, raw_client, user2, mcp) -> None:
    r = await user2.get("/v1/artifact_storage_providers")
    assert r.status_code == 200
    assert "items" in r.json()
    assert (await raw_client.get("/v1/artifact_storage_providers")).status_code == 401
    # The hand-written system tool carries the settings-read semantics on the
    # route; the catalogue's built-in side is not role-filtered, so the user
    # sees the id there, while the MCP side applies the tool's own role floor
    # (admin) and hides it from the non-admin caller.
    scoped = "system__list_artifact_storage_providers"
    r = await user2.get("/v1/tools/catalogue")
    assert scoped in {item["id"] for item in r.json()["items"]}
    bob_id = await _user_id(user2)
    toks = _set_context(role="user", user_id=bob_id)
    try:
        assert scoped not in await _catalogue_ids(mcp)
    finally:
        _reset(toks)
    owner_id = await _user_id(client)
    toks = _set_context(role="admin", user_id=owner_id)
    try:
        assert scoped in await _catalogue_ids(mcp)
        out = await call_tool(mcp, {"scoped_id": scoped, "arguments": {}})
        assert out["ok"] is True, out
        assert "items" in json.loads(out["output"])
        assert call_tool_rejection.get() is None
    finally:
        _reset(toks)


# --- row 6: user, one-to-many (curated) ----------------------------------------------------


async def test_row6_agents_create_lists_both_twins_same_refusal(client, user2, mcp) -> None:
    body = {"name": "m5-parity-agent", "description": "d", "model": {"profile_id": "mp_missing"}}
    r = await user2.get("/v1/tools/catalogue")
    ids = {item["id"] for item in r.json()["items"]}
    assert {"crud__create_agent", "system__create_agent"} <= ids
    # The shared CRUD_TWINS approval gate applies at call time on both twins:
    # the wire envelope and the audit channel are identical.
    bob_id = await _user_id(user2)
    toks = _set_context(role="user", user_id=bob_id)
    try:
        e1 = await call_tool(
            mcp, {"scoped_id": "crud__create_agent", "arguments": {"entity": body}}
        )
        rej1 = call_tool_rejection.get()
        e2 = await call_tool(
            mcp, {"scoped_id": "system__create_agent", "arguments": {"entity": body}}
        )
        rej2 = call_tool_rejection.get()
        assert e1 == e2 == {
            "ok": False,
            "error": {"code": "tool_unavailable", "message": "Tool is unavailable."},
        }
        assert rej1 == rej2 == "approval_required"
    finally:
        _reset(toks)
    # The REST surface has no approval gate: the same body is answered by
    # validation, not by the gate (the gate is a tool-surface control).
    r = await user2.post("/v1/agents", json=body)
    assert r.status_code == 422
    assert r.json()["type"] == "/errors/validation-error"
    assert r.json()["extensions"]["error"] == "model_profile_not_found"


async def test_curated_agent_twins_call_the_same_write_when_the_gate_is_disabled(
    client, user2, mcp, app,
) -> None:
    body = {"name": "m5-parity-agent", "description": "d", "model": {"profile_id": "p--m"}}
    rest = await user2.post("/v1/agents", json={**body, "id": "m5-rest-agent"})
    assert rest.status_code == 201, rest.text
    policies = app.state.storage_provider.get_storage(ToolApprovalPolicy)
    for toolset in ("crud", "system"):
        policy = await policies.get(default_policy_id(toolset, "create_agent"))
        assert policy is not None
        await policies.update(policy.model_copy(update={"enabled": False}))
    app.state.approval_resolver.invalidate()
    tokens = _set_context(role="user", user_id=await _user_id(user2))
    try:
        for toolset in ("crud", "system"):
            entity_id = f"m5-{toolset}-agent"
            out = await call_tool(mcp, {
                "scoped_id": f"{toolset}__create_agent",
                "arguments": {"entity": {**body, "id": entity_id}},
            })
            assert out["ok"] is True, out
            written = json.loads(out["output"])
            assert written["id"] == entity_id
            assert written["model"] == rest.json()["model"]
            assert written["access"] == rest.json()["access"]
            fetched = await user2.get(f"/v1/agents/{entity_id}")
            assert fetched.status_code == 200
            assert fetched.json() == written
            assert call_tool_rejection.get() is None
    finally:
        _reset(tokens)


# --- row 7: user, nested query body (generated) --------------------------------------------


async def test_row7_agents_find_body_identity_and_same_field_refusal(client, user2, mcp) -> None:
    find_body = {"page": {"kind": "offset", "offset": 0, "length": 10}}
    r = await user2.post("/v1/agents/find", json=find_body)
    assert r.status_code == 200
    rest_body = r.json()
    assert rest_body["total"] >= 1
    assert any(item["id"] == "operator" for item in rest_body["items"])
    bob_id = await _user_id(user2)
    toks = _set_context(role="user", user_id=bob_id)
    try:
        # Success-path identity: the generated tool's output IS the route's
        # response body.
        out = await call_tool(
            mcp, {"scoped_id": "agents__find", "arguments": {"body": find_body}}
        )
        assert out["ok"] is True, out
        assert json.loads(out["output"]) == rest_body
        assert call_tool_rejection.get() is None
        # Malformed predicate: REST answers the RFC 7807 problem on the
        # ``predicate`` field; the generated argument schema is stricter than
        # the REST body (kind-discriminated page required, no extra keys), so
        # jsonschema refuses the same field before dispatch. The wire message
        # is the jsonschema wording, not the problem envelope.
        r = await user2.post("/v1/agents/find", json={"predicate": 42, "page": find_body["page"]})
        assert r.status_code == 422
        err = r.json()["extensions"]["errors"][0]
        assert err["loc"] == ["body", "predicate"]
        out = await call_tool(
            mcp,
            {
                "scoped_id": "agents__find",
                "arguments": {"body": {"predicate": 42, "page": find_body["page"]}},
            },
        )
        assert out == {
            "ok": False,
            "error": {
                "code": "invalid_arguments",
                "message": "Arguments do not match the tool schema: 42 is not valid under any of the given schemas",
            },
        }
        assert call_tool_rejection.get() is None
    finally:
        _reset(toks)


# --- row 8: administrator, approval-gated nested body (generated) --------------------------


async def test_row8_internal_collections_role_floor_and_approval_gate(client, user2, mcp, app) -> None:
    ic_body = {
        "embedding_provider_id": "ep_missing",
        "embedding_model": "m",
        "search_provider_id": "ssp_missing",
    }
    r = await user2.put("/v1/internal_collections/config", json=ic_body)
    assert r.status_code == 403
    assert r.json()["type"] == "/errors/forbidden"
    assert r.json()["extensions"]["error"] == "forbidden_role"
    # The tool is hidden from the non-admin by its role floor, so the MCP
    # refusal is the closed identifier, not the 403 wording.
    bob_id = await _user_id(user2)
    toks = _set_context(role="user", user_id=bob_id)
    try:
        assert "internal_collections__put_config" not in await _catalogue_ids(mcp)
        out = await call_tool(
            mcp,
            {"scoped_id": "internal_collections__put_config", "arguments": {"body": ic_body}},
        )
        assert out == {
            "ok": False,
            "error": {"code": "tool_unavailable", "message": "Tool is unavailable."},
        }
        assert call_tool_rejection.get() is None
    finally:
        _reset(toks)
    # The admin caller is visible but the M4 approval gate refuses the call
    # with the existing v1 refusal: no MCP-only continuation.
    owner_id = await _user_id(client)
    toks = _set_context(role="admin", user_id=owner_id)
    try:
        out = await call_tool(
            mcp,
            {"scoped_id": "internal_collections__put_config", "arguments": {"body": ic_body}},
        )
        assert out == {
            "ok": False,
            "error": {"code": "tool_unavailable", "message": "Tool is unavailable."},
        }
        assert call_tool_rejection.get() == "approval_required"
    finally:
        _reset(toks)
    # The M4 preview card stays bounded to its pins (tests/api/test_route_tools.py
    # pins the same tuples against a fresh production build).
    tools = {
        item.tool.id: item.tool
        for item in app.state.generated_route_toolsets.operations
        if item.tool.toolset_id == "internal_collections"
    }
    assert tools["put_config"].preview_args == (
        "body.embedding_provider_id",
        "body.embedding_model",
        "body.search_provider_id",
        "body.cross_encoder",
    )
    assert tools["put_config"].preview_lead == (
        "body.embedding_provider_id",
        "body.embedding_model",
        "body.search_provider_id",
    )


# --- generated runtime failures retain the REST problem envelope --------------------------


async def test_generated_runtime_error_preserves_the_rest_problem_envelope(client, mcp) -> None:
    rest = await client.get("/v1/internal_collections/config")
    assert rest.status_code == 404
    assert rest.headers["content-type"].startswith("application/problem+json")
    tokens = _set_context(role="admin", user_id=await _user_id(client))
    try:
        result = await call_tool(mcp, {
            "scoped_id": "internal_collections__get_config", "arguments": {},
        })
        assert result["ok"] is False
        assert result["error"]["code"] == "tool_failed"
        problem = json.loads(result["error"]["message"])
        expected = rest.json()
        # Each request has its own correlation id; the problem contract is
        # otherwise the exact REST envelope, including instance and status.
        assert problem["extensions"].pop("request_id")
        assert expected["extensions"].pop("request_id")
        assert problem == expected
        assert call_tool_rejection.get() is None
    finally:
        _reset(tokens)


# --- row 9: administrator (generated services create; the matrix "(curated)" label is stale) --


async def test_row9_services_create_user_refused_no_mcp_id_leak(client, user2, mcp) -> None:
    r = await user2.post("/v1/services", json={"name": "svc-m5-parity", "description": "d"})
    assert r.status_code == 403
    assert r.json()["type"] == "/errors/forbidden"
    assert r.json()["extensions"]["error"] == "forbidden_role"
    # Deviation, asserted as actual behavior: the catalogue's built-in side is
    # not role-filtered, so the user's catalogue still lists the admin-only
    # id. The MCP side applies the role floor and must not leak it.
    r = await user2.get("/v1/tools/catalogue")
    assert "services__create" in {item["id"] for item in r.json()["items"]}
    bob_id = await _user_id(user2)
    toks = _set_context(role="user", user_id=bob_id)
    try:
        assert "services__create" not in await _catalogue_ids(mcp)
        out = await call_tool(
            mcp,
            {
                "scoped_id": "services__create",
                "arguments": {"entity": {"name": "svc-m5-parity", "description": "d"}},
            },
        )
        assert out == {
            "ok": False,
            "error": {"code": "tool_unavailable", "message": "Tool is unavailable."},
        }
        assert call_tool_rejection.get() is None
    finally:
        _reset(toks)
    # The admin REST surface succeeds: the refusal is role-scoped, not a door.
    r = await client.post("/v1/services", json={"name": "svc-m5-parity-admin", "description": "d"})
    assert r.status_code == 201


# --- row 10: user, endpoint-enforced visibility ---------------------------------------------


async def test_row10_workspace_visibility_masked_identically(client, raw_client, user2, mcp, app) -> None:
    owner_id = await _user_id(client)
    ws = Workspace(
        id="ws-m5-parity",
        template_id="tpl-m5",
        provider_id="local-1",
        created_at=datetime.now(timezone.utc),
        name="m5-parity",
        runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959", token=SecretStr("stub")),
        access=Access(owners=[owner_id], public=False),
    )
    await app.state.storage_provider.get_storage(Workspace).create(ws)
    # Invisible to the stranger: the missing-row text, not a 403.
    r = await user2.get("/v1/workspaces/ws-m5-parity")
    assert r.status_code == 404
    assert r.json()["type"] == "/errors/not-found"
    assert r.json()["detail"] == "Workspace 'ws-m5-parity' does not exist"
    # Anonymous is refused before the access control runs.
    assert (await raw_client.get("/v1/workspaces/ws-m5-parity")).status_code == 401
    # The list surface scopes the same way.
    r = await user2.get("/v1/workspaces")
    assert "ws-m5-parity" not in {item["id"] for item in r.json()["items"]}
    r = await client.get("/v1/workspaces")
    assert "ws-m5-parity" in {item["id"] for item in r.json()["items"]}
    bob_id = await _user_id(user2)
    toks = _set_context(role="user", user_id=bob_id)
    try:
        # The MCP refusal carries the masked not-found, never the row.
        out = await call_tool(
            mcp, {"scoped_id": "workspaces__get_workspace", "arguments": {"id": "ws-m5-parity"}}
        )
        assert out["ok"] is False
        assert out["error"]["code"] == "tool_failed"
        assert "not-found" in out["error"]["message"]
        assert "ws-m5-parity" in out["error"]["message"]
        assert '"name"' not in out["error"]["message"]
        assert call_tool_rejection.get() is None
    finally:
        _reset(toks)
    # Visible to the owner: 200 on both surfaces, and the MCP output IS the
    # route's response body.
    r = await client.get("/v1/workspaces/ws-m5-parity")
    assert r.status_code == 200
    rest_body = r.json()
    toks = _set_context(role="admin", user_id=owner_id)
    try:
        out = await call_tool(
            mcp, {"scoped_id": "workspaces__get_workspace", "arguments": {"id": "ws-m5-parity"}}
        )
        assert out["ok"] is True, out
        assert json.loads(out["output"]) == rest_body
        assert call_tool_rejection.get() is None
    finally:
        _reset(toks)
