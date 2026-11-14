"""Welcome acknowledgement belongs to the real caller and current role, never an auth epoch."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from primer.auth.tokens import sign_session
from primer.model.except_ import ServerError
from primer.model.user import User
from primer.bootstrap.seed import default_policy_id
from primer.mcp.exposure import ExposureDeps, update_exposure
from primer.mcp.meta_tools import call_tool, call_tool_rejection
from primer.mcp.server import current_actor, current_api_token_scopes, current_principal
from primer.model.principal import Principal
from primer.model.tool_approval import ToolApprovalPolicy


async def person(app, name, role):
    row = await app.state.storage_provider.get_storage(User).create(User(
        id="user-welcome-" + name, username=name, role=role, created_at=datetime.now(UTC),
    ))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    client.cookies.set(app.state.config.auth.cookie_name, sign_session(
        user_id=row.id, username=row.username, secret=app.state.session_secret, epoch=row.session_epoch,
    ))
    return row, client


def test_an_older_user_row_defaults_to_no_acknowledged_roles():
    row = User(id="old", username="old", created_at=datetime.now(UTC))
    assert row.welcome_acked_roles == frozenset()


@pytest.mark.parametrize("action", ["acknowledge", "reopen"])
async def test_generated_mcp_write_has_standard_default_gate_and_same_owner_state(app, action):
    row, client = await person(app, "mcp-" + action, "user")
    storage = app.state.storage_provider.get_storage(User)
    if action == "reopen":
        await storage.update(row.model_copy(update={"welcome_acked_roles": frozenset({"user"})}))
    before = await storage.get(row.id)
    deps = ExposureDeps(
        storage_provider=app.state.storage_provider,
        provider_registry=app.state.provider_registry,
        approval_resolver=app.state.approval_resolver,
    )
    await update_exposure(enabled=True, mode="rbac", allowed_tools=None, updated_by="w3-test", deps=deps)
    tokens = [
        (current_actor, current_actor.set(Principal(type="user", id=row.id, user_id=row.id, display=row.username, role="user", source="local"))),
        (current_api_token_scopes, current_api_token_scopes.set(["mcp"])),
        (current_principal, current_principal.set(row.username)),
    ]
    try:
        arguments = {"scoped_id": "auth__" + action + "_welcome", "arguments": {"body": {"role": "user"}}}
        policies = app.state.storage_provider.get_storage(ToolApprovalPolicy)
        policy = await policies.get(default_policy_id("auth", action + "_welcome"))
        assert policy is not None and policy.enabled
        refused = await call_tool(deps, arguments)
        assert refused["ok"] is False and call_tool_rejection.get() == "approval_required"
        assert (await storage.get(row.id)).welcome_acked_roles == before.welcome_acked_roles
        # An explicitly disabled operator policy permits the SAME generated
        # route; this is not a test-only bypass in the runtime dispatch.
        await policies.update(policy.model_copy(update={"enabled": False}))
        app.state.approval_resolver.invalidate()
        out = await call_tool(deps, arguments)
        assert out["ok"] is True, out
        expected = frozenset({"user"}) if action == "acknowledge" else frozenset()
        assert (await storage.get(row.id)).welcome_acked_roles == expected
        async with client:
            assert (await client.get("/v1/auth/status")).json()["welcome"] == {"variant": "user", "show": action == "reopen"}
    finally:
        for var, token in tokens:
            var.reset(token)
        await client.aclose()


async def test_acknowledge_revisit_reopen_are_durable_and_keep_the_cookie(app):
    row, client = await person(app, "welcome-admin", "admin")
    async with client:
        before = (await client.get("/v1/auth/status")).json()
        assert before["welcome"] == {"variant": "admin", "show": True}
        storage = app.state.storage_provider.get_storage(User)
        original = await storage.get(row.id)
        for _ in range(2):
            response = await client.post("/v1/auth/welcome/acknowledge", json={"role": "admin"})
            assert response.status_code == 200, response.text
            assert response.json() == {"variant": "admin", "show": False}
        saved = await storage.get(row.id)
        assert saved.welcome_acked_roles == {"admin"}
        assert saved.session_epoch == original.session_epoch
        assert saved.password_hash == original.password_hash
        assert saved.default_workspace_id == original.default_workspace_id
        assert (await client.get("/v1/auth/status")).json()["welcome"]["show"] is False
        response = await client.post("/v1/auth/welcome/reopen", json={"role": "admin"})
        assert response.status_code == 200, response.text
        assert response.json() == {"variant": "admin", "show": True}
        assert (await client.get("/v1/auth/status")).json()["authenticated"] is True


async def test_two_people_and_role_transitions_keep_independent_acknowledgements(app):
    row, one = await person(app, "welcome-one", "user")
    _, two = await person(app, "welcome-two", "user")
    storage = app.state.storage_provider.get_storage(User)
    async with one, two:
        assert (await one.post("/v1/auth/welcome/acknowledge", json={"role": "user"})).status_code == 200
        assert (await two.get("/v1/auth/status")).json()["welcome"] == {"variant": "user", "show": True}
        for role, show in (("admin", True), ("user", False), ("admin", False)):
            current = await storage.get(row.id)
            await storage.update(current.model_copy(update={"role": role}))
            assert (await one.get("/v1/auth/status")).json()["welcome"] == {"variant": role, "show": show}
            assert (await one.post("/v1/auth/welcome/acknowledge", json={"role": role})).status_code == 200
        assert (await storage.get(row.id)).welcome_acked_roles == {"admin", "user"}
        assert (await one.post("/v1/auth/welcome/reopen", json={"role": "admin"})).status_code == 200
        assert (await storage.get(row.id)).welcome_acked_roles == {"user"}


@pytest.mark.parametrize("action", ["acknowledge", "reopen"])
async def test_signed_out_restricted_and_auth_disabled_never_persist(app, raw_client, action):
    assert (await raw_client.get("/v1/auth/status")).json()["welcome"] is None
    assert (await raw_client.post("/v1/auth/welcome/" + action, json={"role": "user"})).status_code == 401
    row, restricted = await person(app, "welcome-restricted-" + action, "restricted")
    storage = app.state.storage_provider.get_storage(User)
    async with restricted:
        assert (await restricted.get("/v1/auth/status")).json()["welcome"] is None
        assert (await restricted.post("/v1/auth/welcome/" + action, json={"role": "user"})).status_code == 403
    app.state.config = app.state.config.model_copy(update={
        "auth": app.state.config.auth.model_copy(update={"enabled": False}),
    })
    assert (await raw_client.get("/v1/auth/status")).json()["welcome"] is None
    assert (await raw_client.post("/v1/auth/welcome/" + action, json={"role": "admin"})).status_code == 400
    assert (await storage.get(row.id)).welcome_acked_roles == frozenset()


async def test_a_stale_role_and_a_failed_write_never_acknowledge(app, monkeypatch):
    row, client = await person(app, "welcome-failure", "user")
    storage = app.state.storage_provider.get_storage(User)
    async with client:
        response = await client.post("/v1/auth/welcome/acknowledge", json={"role": "admin"})
        assert response.status_code == 409, response.text
        assert response.headers["content-type"].startswith("application/problem+json")

        async def unavailable(*_args, **_kwargs):
            raise ServerError("welcome store unavailable")

        monkeypatch.setattr(storage, "patch_if", unavailable)
        response = await client.post("/v1/auth/welcome/acknowledge", json={"role": "user"})
        assert response.status_code >= 500, response.text
        assert response.headers["content-type"].startswith("application/problem+json")
        assert (await storage.get(row.id)).welcome_acked_roles == frozenset()


async def test_a_role_change_between_read_and_write_is_rechecked_without_lost_fields(app, monkeypatch):
    row, client = await person(app, "welcome-race", "user")
    storage = app.state.storage_provider.get_storage(User)
    real = storage.patch_if
    raced = False

    async def race(id, patch=None, *, where, **kwargs):
        nonlocal raced
        if "welcome_acked_roles" in patch and not raced:
            raced = True
            await real(id, {"role": "admin", "session_epoch": 1, "email": "kept@example.com"}, where={"session_epoch": [0]})
        return await real(id, patch, where=where, **kwargs)

    monkeypatch.setattr(storage, "patch_if", race)
    async with client:
        response = await client.post("/v1/auth/welcome/acknowledge", json={"role": "user"})
        assert response.status_code == 409, response.text
    saved = await storage.get(row.id)
    assert raced and saved.welcome_acked_roles == frozenset()
    assert saved.role == "admin" and saved.session_epoch == 1 and saved.email == "kept@example.com"
