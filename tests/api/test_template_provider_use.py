"""A workspace template may name only a provider its SAVER may use (access-control P4 Task 5, plan Decision 4).

An unusable provider reads like a missing one; an unchanged provider is not re-checked, so an edit by a full grantee checks only what it adds.
The ``create_workspace_template`` tool answers as the route does.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
from httpx import ASGITransport

from primer.auth.tokens import sign_session
from primer.model.access import Access
from primer.model.principal import PrincipalRef
from primer.model.user import User
from primer.model.workspace import LocalWorkspaceConfig, WorkspaceProvider, WorkspaceProviderType, WorkspaceTemplate
from primer.model.yield_ import ToolContext


async def _user_client(app, uid: str) -> httpx.AsyncClient:
    user = await app.state.storage_provider.get_storage(User).create(
        User(id=uid, username=uid, password_hash=None, created_at=datetime.now(timezone.utc), role="user")
    )
    c = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    c.cookies.set(app.state.config.auth.cookie_name, sign_session(
        user_id=user.id, username=user.username, secret=app.state.session_secret, src="local", epoch=user.session_epoch))
    return c


async def _providers(app) -> None:
    store = app.state.storage_provider.get_storage(WorkspaceProvider)
    for pid, access in (("p-private", Access()), ("p-public", Access(public=True)), ("p-granted", Access(grants={"u-ada": "read"}, rev=1))):
        await store.create(WorkspaceProvider(
            id=pid, provider=WorkspaceProviderType.LOCAL, config=LocalWorkspaceConfig(root_path="/tmp/primer-ws-tests"), access=access,
        ))


def _body(template_id: str, provider_id: str) -> dict:
    return {"id": template_id, "description": "t", "provider_id": provider_id}


async def test_a_user_cannot_save_a_template_on_a_provider_they_may_not_use(app):
    await _providers(app)
    ada = await _user_client(app, "u-ada")
    async with ada:
        refused = await ada.post("/v1/workspace_templates", json=_body("t-private", "p-private"))
        assert refused.status_code == 422, refused.text
        assert refused.json()["extensions"]["error"] == "workspace_provider_not_found"
        missing = await ada.post("/v1/workspace_templates", json=_body("t-missing", "p-nope"))
        assert missing.status_code == 422 and missing.json()["extensions"]["error"] == "workspace_provider_not_found"
        assert (await ada.post("/v1/workspace_templates", json=_body("t-public", "p-public"))).status_code == 201
        assert (await ada.post("/v1/workspace_templates", json=_body("t-granted", "p-granted"))).status_code == 201


async def test_an_admin_names_any_provider(client, app):
    await _providers(app)

    assert (await client.post("/v1/workspace_templates", json=_body("t-admin", "p-private"))).status_code == 201


async def test_an_edit_that_keeps_the_provider_is_not_rechecked(app):
    await _providers(app)
    await app.state.storage_provider.get_storage(WorkspaceTemplate).create(WorkspaceTemplate.model_validate({
        **_body("t-1", "p-private"), "access": {"owners": ["u-owner"], "grants": {"u-bob": "full"}, "members": ["u:u-owner", "u:u-bob"]},
    }))
    bob = await _user_client(app, "u-bob")
    async with bob:
        body = (await bob.get("/v1/workspace_templates/t-1")).json()
        assert (await bob.put("/v1/workspace_templates/t-1", json={**body, "description": "edited"})).status_code == 200
        refused = await bob.put("/v1/workspace_templates/t-1", json={**body, "provider_id": "p-private-2"})
        assert refused.status_code == 422 and refused.json()["extensions"]["error"] == "workspace_provider_not_found"


async def test_the_tool_twin_answers_as_the_route_does(app):
    from primer.api.registries import WorkspaceRegistry
    from primer.toolset.workspaces import build_workspaces_toolset

    await _providers(app)
    sp = app.state.storage_provider
    toolset = build_workspaces_toolset(storage_provider=sp, workspace_registry=WorkspaceRegistry(sp))
    ada = PrincipalRef(type="user", id="u-ada", display="ada", role="user", source="local", user_id="u-ada")
    ctx = ToolContext(tool_call_id="c", session_id=None, workspace_id=None, initiated_by=ada)

    refused = await toolset.call(tool_name="create_workspace_template", arguments={"entity": _body("tt-private", "p-private")}, ctx=ctx)
    ok = await toolset.call(tool_name="create_workspace_template", arguments={"entity": _body("tt-public", "p-public")}, ctx=ctx)

    assert refused.is_error and "does not exist" in refused.output and "WorkspaceProvider" in refused.output
    assert not ok.is_error, ok.output
