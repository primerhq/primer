"""The per-user main operator session ensure route (pivot plan P2 task 6, lead ruling 3).

``POST /v1/workspaces/{id}/main_session`` returns the caller's own open operator session on the workspace, creating
it as the caller when they have none (role user). The console opens the default workspace on first login and picks
the result (task 14). The session runs at the caller's rank: ``initiated_by`` carries the caller's role, so an
admin's main session clears the admin role floor and a user's does not (A-20). The status answers 201 when this call
created the row and 200 when it returned the caller's existing one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport

from primer.bootstrap.defaults import (
    RESERVED_LOCAL_WORKSPACE_PROVIDER,
    RESERVED_LOCAL_WORKSPACE_TEMPLATE,
    RESERVED_WORKSPACE_TEMPLATES,
)
from primer.bootstrap.seed import ensure_default_workspace
from primer.model.storage import FieldRef, OffsetPage, Op, Predicate, Value
from primer.model.user import User
from primer.model.workspace import (
    LocalWorkspaceConfig,
    WorkspaceProvider,
    WorkspaceProviderType,
    WorkspaceTemplate,
)
from primer.model.workspace_session import WorkspaceSession


@pytest_asyncio.fixture
async def prime_app(app: FastAPI, tmp_path) -> AsyncIterator[FastAPI]:
    """The test app with the reserved local template + provider seeded and the default workspace created and
    recorded by the ensure pass, as the lifespan does."""
    sp = app.state.storage_provider
    templates = sp.get_storage(WorkspaceTemplate)
    if await templates.get(RESERVED_LOCAL_WORKSPACE_TEMPLATE) is None:
        await templates.create(
            WorkspaceTemplate(
                **{
                    **RESERVED_WORKSPACE_TEMPLATES[RESERVED_LOCAL_WORKSPACE_TEMPLATE],
                    "id": RESERVED_LOCAL_WORKSPACE_TEMPLATE,
                }
            )
        )
    providers = sp.get_storage(WorkspaceProvider)
    if await providers.get(RESERVED_LOCAL_WORKSPACE_PROVIDER) is None:
        await providers.create(
            WorkspaceProvider(
                id=RESERVED_LOCAL_WORKSPACE_PROVIDER,
                provider=WorkspaceProviderType.LOCAL,
                config=LocalWorkspaceConfig(root_path=str(tmp_path / "prime-ws")),
            )
        )
    created = await ensure_default_workspace(sp, workspace_registry=app.state.workspace_registry)
    assert created == "prime", "the default workspace must exist for the console's first login"
    yield app


async def _default_id(app: FastAPI) -> str:
    """The same read the console makes: the recorded id, never a literal."""
    return (await app.state.storage_provider.get_system_state()).default_workspace_id


async def _sessions(app: FastAPI, wid: str) -> list[WorkspaceSession]:
    page = await app.state.storage_provider.get_storage(WorkspaceSession).find(
        Predicate(
            left=FieldRef(name="workspace_id"), op=Op.EQ, right=Value(value=wid),
        ),
        OffsetPage(offset=0, length=50),
    )
    return list(page.items)


async def _user_id(app: FastAPI, username: str) -> str:
    page = await app.state.storage_provider.get_storage(User).find(
        Predicate(
            left=FieldRef(name="username"), op=Op.EQ, right=Value(value=username),
        ),
        OffsetPage(offset=0, length=1),
    )
    return page.items[0].id


@pytest.mark.asyncio
async def test_ensures_the_callers_main_session(prime_app, client) -> None:
    wid = await _default_id(prime_app)

    r = await client.post(f"/v1/workspaces/{wid}/main_session")

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "main" and body["workspace_id"] == wid
    assert body["binding"]["agent_id"] == "operator"
    assert body["status"] == "created"
    # The session runs as the caller: the projection carries the caller's role.
    assert body["initiated_by"]["type"] == "user"
    assert body["initiated_by"]["role"] == "admin"
    assert body["initiated_by"]["id"] == await _user_id(prime_app, "testuser")
    rows = await _sessions(prime_app, wid)
    assert len(rows) == 1 and rows[0].id == body["id"]


@pytest.mark.asyncio
async def test_a_second_call_returns_the_same_session(prime_app, client) -> None:
    wid = await _default_id(prime_app)

    first = (await client.post(f"/v1/workspaces/{wid}/main_session")).json()
    again = await client.post(f"/v1/workspaces/{wid}/main_session")

    assert again.status_code == 200
    assert again.json()["id"] == first["id"]
    assert len(await _sessions(prime_app, wid)) == 1


@pytest.mark.asyncio
async def test_each_user_gets_their_own_session(prime_app, client) -> None:
    wid = await _default_id(prime_app)
    admin_session = (await client.post(f"/v1/workspaces/{wid}/main_session")).json()

    r = await client.post("/v1/admin/users", json={"username": "carol", "password": "carolpw1"})
    assert r.status_code == 201, r.text
    async with httpx.AsyncClient(
        transport=ASGITransport(app=prime_app), base_url="http://test",
    ) as carol:
        login = await carol.post("/v1/auth/login", json={"username": "carol", "password": "carolpw1"})
        assert login.status_code == 200, login.text

        cr = await carol.post(f"/v1/workspaces/{wid}/main_session")
        assert cr.status_code == 201, cr.text
        cbody = cr.json()
        assert cbody["id"] != admin_session["id"]
        assert cbody["initiated_by"]["role"] == "user"
        assert cbody["initiated_by"]["display"] == "carol"

        again = await carol.post(f"/v1/workspaces/{wid}/main_session")
        assert again.status_code == 200 and again.json()["id"] == cbody["id"]

    # Two main sessions on the workspace, one per user; the admin still gets the admin's.
    rows = await _sessions(prime_app, wid)
    assert sorted(r.id for r in rows) == sorted({admin_session["id"], cbody["id"]})
    admin_again = await client.post(f"/v1/workspaces/{wid}/main_session")
    assert admin_again.status_code == 200
    assert admin_again.json()["id"] == admin_session["id"]


@pytest.mark.asyncio
async def test_an_unknown_workspace_404s(prime_app, client) -> None:
    r = await client.post("/v1/workspaces/does-not-exist/main_session")
    assert r.status_code == 404
    assert (await _sessions(prime_app, await _default_id(prime_app))) == []
