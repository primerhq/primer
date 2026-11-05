"""The wire error shape of the console-facing refusals (G15-01 / G15-02 / G15-04).

The problem handler reduces an object ``detail`` to the envelope's string ``detail``
(primer/api/errors.py ``_detail_from_mapping``): it prefers a ``message`` key and
falls back to the machine code. The auth gate and the API-token routes carried no
``message``, so the wire ``detail`` was the bare code ("auth_required",
"forbidden_role", "scope_required", "approver_mismatch", "invalid_credentials"); the
API-token refusals answered raw application/json ``{"code", "message"}`` outside the
problem+json envelope (no type, no title, no request id); and the two
subsystem-inactive 503s put the problem's own type/title keys inside the detail dict,
so the wire detail was the status title and the reason sat in extensions.title, where
no reader looks.

These pin the wire contract per code family: the detail is a sentence, the machine
code stays in extensions for machine consumers, the token refusals ride the
problem+json envelope, and the 503s name the registry while keeping the
/errors/subsystem-inactive type URI. RED at the base commit (the shapes are the
bare-code / raw-json ones); GREEN once the message rides with the refusal.

Audit: /var/tmp/revamp/audit/G15.md (G15-01, G15-02, G15-04).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from primer.api._app_middleware import _install_request_id
from primer.api.approver_guard import enforce_approvers
from primer.api.deps import (
    get_artifact_storage_registry,
    get_semantic_search_registry,
    require_admin,
    require_scope,
    require_user,
)
from primer.api.errors import PROBLEM_JSON_MEDIA_TYPE, register_error_handlers
from primer.api.routers.tap import tap_router
from primer.model.user import User
from primer.session.approvers import ADMIN_ONLY_METADATA

AUTH_REQUIRED = "Your session has ended; sign in again."
FORBIDDEN_ROLE = "Your role does not allow this."
INVALID_CREDENTIALS = "The username or password is wrong."
SCOPE_REQUIRED = "This API token lacks the required scope."
APPROVER_MISMATCH = "Only a named approver or an admin may decide this gate."
SEMANTIC_INACTIVE = "The semantic-search registry is not configured on this server."
ARTIFACT_INACTIVE = "The artifact storage registry is not configured on this server."
TAP_ROUTER_INACTIVE = "The workspace tap router is not configured on this server."
WORKSPACE_REGISTRY_INACTIVE = "The workspace registry is not configured on this server."
MANAGED_FIELD_SET = "The harness_id field is set by the managing harness and cannot be set at create."


def _gated_app() -> TestClient:
    """The real require_user / require_admin / require_scope deps and the real
    approver guard, behind the real problem handler, with the request id threaded."""
    app = FastAPI()
    register_error_handlers(app)
    _install_request_id(app)

    @app.middleware("http")
    async def _sign_in(request: Request, call_next):
        role = request.headers.get("x-test-role")
        if role:
            request.state.user = User(
                id="u-1", username="someone", role=role,
                created_at=datetime.now(timezone.utc),
            )
        scopes = request.headers.get("x-test-token-scopes")
        if scopes is not None:
            request.state.api_token = SimpleNamespace(scopes=scopes.split(","))
        return await call_next(request)

    @app.post("/v1/gated", dependencies=[Depends(require_user)])
    def gated():
        return {}

    @app.post("/v1/admin_gated", dependencies=[Depends(require_admin)])
    def admin_gated():
        return {}

    @app.post("/v1/scoped", dependencies=[require_scope("mcp")])
    def scoped():
        return {}

    @app.post("/v1/gate")
    def gate(request: Request):
        enforce_approvers(ADMIN_ONLY_METADATA, getattr(request.state, "user", None))
        return {}

    return TestClient(app, raise_server_exceptions=False)


# ---- G15-01: the wire detail is the sentence, the code stays in extensions -------------------------------------------


def test_auth_required_ships_the_session_sentence_with_the_code_in_extensions():
    r = _gated_app().post("/v1/gated")

    assert r.status_code == 401
    assert r.headers["content-type"] == PROBLEM_JSON_MEDIA_TYPE
    body = r.json()
    assert body["detail"] == AUTH_REQUIRED, "the wire detail is the bare code where a sentence belongs"
    assert body["extensions"]["error"] == "auth_required"
    assert "code" not in body["extensions"]


def test_require_admin_says_the_same_session_sentence():
    r = _gated_app().post("/v1/admin_gated")

    assert r.status_code == 401
    body = r.json()
    assert body["detail"] == AUTH_REQUIRED
    assert body["extensions"]["error"] == "auth_required"


def test_forbidden_role_ships_the_role_sentence_with_the_code_in_extensions():
    c = _gated_app()
    for path in ("/v1/gated", "/v1/admin_gated"):
        r = c.post(path, headers={"x-test-role": "restricted"})
        assert r.status_code == 403, (path, r.text)
        body = r.json()
        assert body["detail"] == FORBIDDEN_ROLE, (path, r.text)
        assert body["extensions"]["error"] == "forbidden_role", (path, r.text)
    # an admin passes both gates: the fix must not reject a role that may do this
    assert c.post("/v1/gated", headers={"x-test-role": "admin"}).status_code == 200


def test_scope_required_ships_the_scope_sentence_and_keeps_the_scope_in_extensions():
    c = _gated_app()
    assert c.post("/v1/scoped", headers={"x-test-token-scopes": "mcp,read"}).status_code == 200
    r = c.post("/v1/scoped", headers={"x-test-token-scopes": "read"})

    assert r.status_code == 403, r.text
    body = r.json()
    assert body["detail"] == SCOPE_REQUIRED, "the wire detail is the bare code where a sentence belongs"
    assert body["extensions"]["code"] == "scope_required"
    assert body["extensions"]["scope"] == "mcp"


def test_approver_mismatch_ships_the_approvers_sentence_with_the_code_in_extensions():
    c = _gated_app()
    r = c.post("/v1/gate", headers={"x-test-role": "user"})

    assert r.status_code == 403, r.text
    body = r.json()
    assert body["detail"] == APPROVER_MISMATCH, "the wire detail is the bare code where a sentence belongs"
    assert body["extensions"]["error"] == "approver_mismatch"
    # an admin may decide the gate: the guard still admits the role that may
    assert c.post("/v1/gate", headers={"x-test-role": "admin"}).status_code == 200


@pytest.mark.asyncio
async def test_invalid_credentials_ships_the_login_sentence_with_the_code_in_extensions(client):
    r = await client.post(
        "/v1/auth/login",
        json={"username": "testuser", "password": "not-the-password"},
    )

    assert r.status_code == 401, r.text
    assert r.headers["content-type"] == PROBLEM_JSON_MEDIA_TYPE
    body = r.json()
    assert body["detail"] == INVALID_CREDENTIALS, "the wire detail is the bare code where a sentence belongs"
    assert body["extensions"]["error"] == "invalid_credentials"


# ---- G15-02: the API-token refusals ride the problem+json envelope ------------------------------------------------------


def _token_problem(r, *, status: int, type_uri: str, title: str, code: str, message: str) -> None:
    assert r.status_code == status, r.text
    assert r.headers["content-type"] == PROBLEM_JSON_MEDIA_TYPE, (
        f"the refusal left the problem+json envelope: {r.headers.get('content-type')!r}"
    )
    body = r.json()
    assert body["type"] == type_uri, body
    assert body["title"] == title, body
    assert body["status"] == status, body
    assert body["detail"] == message, "the message must ride the wire detail"
    assert body["extensions"]["code"] == code, body
    assert body["extensions"]["message"] == message, body
    assert "request_id" in body["extensions"], "the envelope must thread the request id"
    assert body.get("instance"), "the envelope must carry the instance"


@pytest.mark.asyncio
async def test_create_empty_name_refusal_is_the_problem_envelope(client):
    r = await client.post("/v1/auth/tokens", json={"name": "   "})

    _token_problem(
        r, status=422, type_uri="/errors/validation-error", title="Validation Error",
        code="token_name_empty", message="name must be non-empty after stripping whitespace",
    )


@pytest.mark.asyncio
async def test_create_expires_in_past_refusal_is_the_problem_envelope(client):
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    r = await client.post("/v1/auth/tokens", json={"name": "expired", "expires_at": past})

    _token_problem(
        r, status=422, type_uri="/errors/validation-error", title="Validation Error",
        code="token_expires_in_past", message="expires_at must be in the future",
    )


@pytest.mark.asyncio
async def test_create_name_conflict_refusal_is_the_problem_envelope(client):
    assert (await client.post("/v1/auth/tokens", json={"name": "dup"})).status_code == 201
    r = await client.post("/v1/auth/tokens", json={"name": "dup"})

    _token_problem(
        r, status=409, type_uri="/errors/conflict", title="Conflict",
        code="token_name_conflict", message="a token named 'dup' already exists",
    )


@pytest.mark.asyncio
async def test_rename_empty_name_refusal_is_the_problem_envelope(client):
    created = await client.post("/v1/auth/tokens", json={"name": "renamable"})
    assert created.status_code == 201, created.text
    tid = created.json()["id"]
    r = await client.put(f"/v1/auth/tokens/{tid}", json={"name": "   "})

    _token_problem(
        r, status=422, type_uri="/errors/validation-error", title="Validation Error",
        code="token_name_empty", message="name must be non-empty after stripping whitespace",
    )


@pytest.mark.asyncio
async def test_rename_conflict_refusal_is_the_problem_envelope(client):
    assert (await client.post("/v1/auth/tokens", json={"name": "alpha"})).status_code == 201
    created = await client.post("/v1/auth/tokens", json={"name": "beta"})
    assert created.status_code == 201, created.text
    tid = created.json()["id"]
    r = await client.put(f"/v1/auth/tokens/{tid}", json={"name": "alpha"})

    _token_problem(
        r, status=409, type_uri="/errors/conflict", title="Conflict",
        code="token_name_conflict", message="a token named 'alpha' already exists",
    )


# ---- G15-04: the subsystem-inactive 503s name the registry and keep the type URI ----------------------------------------


def _subsystem_probe(dep) -> TestClient:
    """One route that depends on the registry dep; the test app never wires the registry at lifespan."""
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/v1/probe")
    def probe(registry=Depends(dep)):
        return {}

    return TestClient(app, raise_server_exceptions=False)


def test_semantic_search_inactive_503_names_the_registry_and_keeps_the_type_uri():
    r = _subsystem_probe(get_semantic_search_registry).get("/v1/probe")

    assert r.status_code == 503, r.text
    assert r.headers["content-type"] == PROBLEM_JSON_MEDIA_TYPE, r.text
    body = r.json()
    assert body["detail"] == SEMANTIC_INACTIVE, (
        "the detail is the status title; the registry's name never reaches the reader"
    )
    assert body["extensions"]["type"] == "/errors/subsystem-inactive", body
    assert body["extensions"]["message"] == SEMANTIC_INACTIVE, "the reason must sit where the reader looks"
    assert "title" not in body["extensions"], "the dead title key is where no reader looks"


def test_artifact_storage_inactive_503_names_the_registry_and_keeps_the_type_uri():
    r = _subsystem_probe(get_artifact_storage_registry).get("/v1/probe")

    assert r.status_code == 503, r.text
    assert r.headers["content-type"] == PROBLEM_JSON_MEDIA_TYPE, r.text
    body = r.json()
    assert body["detail"] == ARTIFACT_INACTIVE, (
        "the detail is the status title; the registry's name never reaches the reader"
    )
    assert body["extensions"]["type"] == "/errors/subsystem-inactive", body
    assert body["extensions"]["message"] == ARTIFACT_INACTIVE, "the reason must sit where the reader looks"
    assert "title" not in body["extensions"], "the dead title key is where no reader looks"


def _principal(request: Request) -> None:
    """Seed the request principal the real tap route reads before its 503s.

    A ``user``-role account with an actor the Person derives from; the
    workspace row below grants READ (public), so the route reaches the
    subsystem-inactive checks."""
    request.state.user = User.model_construct(id="user-1", username="op", role="user")
    request.state.actor = SimpleNamespace(type="user", user_id="user-1", role="user")


class _TapProbeRowStorage:
    """Answers the route's one workspace read with a public (READ-granting) row."""

    async def get(self, workspace_id):
        return SimpleNamespace(access=SimpleNamespace(owners=[], grants={}, public=True))


class _TapProbeStorage:
    """The app.state.storage_provider the real route's require_workspace line reads."""

    def get_storage(self, model):
        return _TapProbeRowStorage()


def _tap_real_route_probe() -> TestClient:
    """The REAL workspace_tap route (primer/api/routers/tap.py) behind the
    real problem handler: the pin exercises the actual raising code, not a
    re-implementation of the condition. The route's pre-503 lines need
    only what the probe seeds (request.state.user / .actor, an
    app.state.storage_provider whose row grants READ); the
    workspace_tap_router / workspace_registry app-state attributes are
    left unwired, which is exactly the condition under test (a server
    built without those subsystems)."""
    app = FastAPI(dependencies=[Depends(_principal)])
    register_error_handlers(app)
    app.state.storage_provider = _TapProbeStorage()
    app.include_router(tap_router)
    return TestClient(app, raise_server_exceptions=False)


def test_workspace_tap_router_inactive_503_names_the_router_and_keeps_the_type_uri():
    r = _tap_real_route_probe().get("/workspaces/ws-1/tap")

    assert r.status_code == 503, r.text
    assert r.headers["content-type"] == PROBLEM_JSON_MEDIA_TYPE, r.text
    body = r.json()
    assert body["detail"] == TAP_ROUTER_INACTIVE, (
        "the detail is the status title; the router's name never reaches the reader"
    )
    assert body["extensions"]["type"] == "/errors/subsystem-inactive", body
    assert body["extensions"]["message"] == TAP_ROUTER_INACTIVE, "the reason must sit where the reader looks"
    assert "title" not in body["extensions"], "the dead title key is where no reader looks"


def test_workspace_registry_inactive_503_names_the_registry_and_keeps_the_type_uri():
    probe = _tap_real_route_probe()
    probe.app.state.workspace_tap_router = object()  # get past the first check
    r = probe.get("/workspaces/ws-1/tap")

    assert r.status_code == 503, r.text
    assert r.headers["content-type"] == PROBLEM_JSON_MEDIA_TYPE, r.text
    body = r.json()
    assert body["detail"] == WORKSPACE_REGISTRY_INACTIVE, (
        "the detail is the status title; the registry's name never reaches the reader"
    )
    assert body["extensions"]["type"] == "/errors/subsystem-inactive", body
    assert body["extensions"]["message"] == WORKSPACE_REGISTRY_INACTIVE, "the reason must sit where the reader looks"
    assert "title" not in body["extensions"], "the dead title key is where no reader looks"


# ---- G15R-01: the last bare-code member of G15-01 (primer/api/routers/_managed.py) --------------------------------------


def _managed_agent_body() -> dict:
    """A real agent-create body that sets the harness-managed field."""
    from primer.model.agent import Agent, AgentModel

    return Agent(
        id="new-agent-g15r",
        description="g15r pin",
        model=AgentModel(profile_id="llm-1--gpt-4o"),
        harness_id="h-pin",
    ).model_dump(mode="json")


async def test_managed_field_set_422_ships_the_field_sentence_with_the_code_in_extensions(client):
    r = await client.post("/v1/agents", json=_managed_agent_body())

    assert r.status_code == 422, r.text
    assert r.headers["content-type"] == PROBLEM_JSON_MEDIA_TYPE, r.text
    body = r.json()
    assert body["detail"] == MANAGED_FIELD_SET, (
        "the wire detail is the bare code where a sentence belongs"
    )
    assert body["extensions"]["error"] == "managed_field_set", body
    assert body["extensions"]["field"] == "harness_id", body
    assert "title" not in body["extensions"], "the dead title key is where no reader looks"
