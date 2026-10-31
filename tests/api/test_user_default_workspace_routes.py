"""Sign-in gives a full user their private default workspace; /auth/status exposes it (access-control P2 Task 8)."""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
from httpx import ASGITransport

from primer.auth.passwords import hash_password
from primer.model.user import User
from primer.model.workspace import Workspace


class _Live:
    def __init__(self, workspace_id):
        self.id = workspace_id
        from primer.model.workspace import WorkspaceRuntimeMeta

        self.runtime_meta = WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959", token="t")


@pytest.fixture
async def stub_materialise(app, monkeypatch):
    from primer.bootstrap.defaults import (
        RESERVED_LOCAL_WORKSPACE_TEMPLATE,
        RESERVED_OPERATOR_AGENT,
        RESERVED_WORKSPACE_TEMPLATES,
    )
    from primer.model.agent import Agent, AgentModel
    from primer.model.workspace import WorkspaceTemplate

    sp = app.state.storage_provider
    if await sp.get_storage(WorkspaceTemplate).get(RESERVED_LOCAL_WORKSPACE_TEMPLATE) is None:
        await sp.get_storage(WorkspaceTemplate).create(
            WorkspaceTemplate(**RESERVED_WORKSPACE_TEMPLATES[RESERVED_LOCAL_WORKSPACE_TEMPLATE])
        )
    if await sp.get_storage(Agent).get(RESERVED_OPERATOR_AGENT) is None:
        await sp.get_storage(Agent).create(
            Agent(id=RESERVED_OPERATOR_AGENT, description="op", model=AgentModel(profile_id="p--m"), tools=[], system_prompt=[])
        )
    calls: list[str] = []

    async def materialise(*, template, overrides=None, workspace_id=None, person=None):
        assert person is not None and person.user_id and workspace_id.endswith(person.user_id), person  # made as its user
        calls.append(workspace_id)
        return _Live(workspace_id)

    monkeypatch.setattr(app.state.workspace_registry, "materialise", materialise)
    return calls


async def _user_client(app, user_id: str, role: str, password: str = "pw-for-tests-123") -> httpx.AsyncClient:
    await app.state.storage_provider.get_storage(User).create(User(
        id=user_id, username=user_id, password_hash=await hash_password(password),
        created_at=datetime.now(timezone.utc), role=role))
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test"), password


async def test_password_login_creates_the_default_and_status_exposes_it(app, stub_materialise):
    c, pw = await _user_client(app, "user-ada", "user")
    async with c:
        assert (await c.post("/v1/auth/login", json={"username": "user-ada", "password": pw})).status_code == 200
        status = (await c.get("/v1/auth/status")).json()
        assert status["default_workspace_id"] == "ws-user-ada"
        again = await c.post("/v1/auth/login", json={"username": "user-ada", "password": pw})
        assert again.status_code == 200
        listed = (await c.get("/v1/workspaces")).json()["items"]
    assert stub_materialise == ["ws-user-ada"]
    flagged = [w["id"] for w in listed if w["is_default"]]
    assert flagged == ["ws-user-ada"]


async def test_a_restricted_user_has_none_until_promoted_and_status_creates_it(app, stub_materialise):
    sp = app.state.storage_provider
    c, pw = await _user_client(app, "user-rex", "restricted")
    async with c:
        await c.post("/v1/auth/login", json={"username": "user-rex", "password": pw})
        assert (await c.get("/v1/auth/status")).json()["default_workspace_id"] is None
        assert stub_materialise == []
        users = sp.get_storage(User)
        row = await users.get("user-rex")
        await users.update(row.model_copy(update={"role": "user"}))
        status = (await c.get("/v1/auth/status")).json()
    assert status["default_workspace_id"] == "ws-user-rex"


async def test_a_users_default_workspace_cannot_be_deleted(app, stub_materialise):
    c, pw = await _user_client(app, "user-ada", "user")
    async with c:
        await c.post("/v1/auth/login", json={"username": "user-ada", "password": pw})
        resp = await c.delete("/v1/workspaces/ws-user-ada")
    assert resp.status_code in (403, 409), resp.text
    assert await app.state.storage_provider.get_storage(Workspace).get("ws-user-ada") is not None


async def test_a_failing_ensure_is_not_retried_by_status_within_the_backoff(app, stub_materialise, monkeypatch):
    from primer.api import user_default

    calls = []

    async def boom(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("backend down")

    monkeypatch.setattr(user_default, "ensure_user_default_workspace", boom)
    user_default._FAILED_UNTIL.clear()
    c, pw = await _user_client(app, "user-ada", "user")
    async with c:
        await c.post("/v1/auth/login", json={"username": "user-ada", "password": pw})  # a sign-in always tries
        assert len(calls) == 1
        for _ in range(2):
            assert (await c.get("/v1/auth/status")).json()["default_workspace_id"] is None
        assert len(calls) == 1  # status inside the 60 s window did not retry
        user_default._FAILED_UNTIL.clear()
        await c.get("/v1/auth/status")
        assert len(calls) == 2
    user_default._FAILED_UNTIL.clear()
