"""An agent or graph may name only a profile its SAVER may use (access-control P4 Task 4, plan Decision 4).

An unusable profile answers exactly like a missing one (422 ``model_profile_not_found``). An unchanged reference is not re-checked, so an
edit by a full grantee checks the grantee on what the edit ADDS. The tools answer as the routes do.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
from httpx import ASGITransport

from primer.auth.tokens import sign_session
from primer.model.access import Access
from primer.model.agent import Agent
from primer.model.model_profile import ModelProfile
from primer.model.principal import PrincipalRef
from primer.model.user import User
from primer.model.yield_ import ToolContext


async def _user_client(app, uid: str) -> httpx.AsyncClient:
    user = await app.state.storage_provider.get_storage(User).create(
        User(id=uid, username=uid, password_hash=None, created_at=datetime.now(timezone.utc), role="user")
    )
    c = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    c.cookies.set(app.state.config.auth.cookie_name, sign_session(
        user_id=user.id, username=user.username, secret=app.state.session_secret, src="local", epoch=user.session_epoch))
    return c


async def _profile(app, pid: str, access: Access) -> None:
    await app.state.storage_provider.get_storage(ModelProfile).create(
        ModelProfile(id=pid, description=pid, provider_id="p", model_name="m", context_length=1000, access=access)
    )


def _agent(agent_id: str, profile_id: str) -> dict:
    return {"id": agent_id, "description": "d", "model": {"profile_id": profile_id}}


async def test_a_user_cannot_create_an_agent_on_a_profile_they_may_not_use(app):
    await _profile(app, "pro-private", Access())
    await _profile(app, "pro-granted", Access(grants={"u-ada": "read"}, rev=1))
    await _profile(app, "pro-public", Access(public=True))
    ada = await _user_client(app, "u-ada")
    async with ada:
        refused = await ada.post("/v1/agents", json=_agent("ag-private", "pro-private"))
        assert refused.status_code == 422, refused.text
        assert refused.json()["extensions"]["error"] == "model_profile_not_found"
        assert (await ada.post("/v1/agents", json=_agent("ag-granted", "pro-granted"))).status_code == 201
        assert (await ada.post("/v1/agents", json=_agent("ag-public", "pro-public"))).status_code == 201
        missing = await ada.post("/v1/agents", json=_agent("ag-missing", "pro-nope"))
        # An unusable profile reads exactly like a missing one: the same status, code and field.
        assert missing.status_code == 422
        assert missing.json()["extensions"]["error"] == refused.json()["extensions"]["error"]
        assert missing.json()["extensions"]["field"] == refused.json()["extensions"]["field"]


async def test_an_admin_names_any_profile(client, app):
    await _profile(app, "pro-private", Access())

    assert (await client.post("/v1/agents", json=_agent("ag-admin", "pro-private"))).status_code == 201


async def test_an_edit_by_a_full_grantee_checks_the_grantee_on_what_it_adds(app):
    await _profile(app, "pro-private", Access())
    await _profile(app, "pro-public", Access(public=True))
    await app.state.storage_provider.get_storage(Agent).create(
        Agent.model_validate({
            **_agent("ag-1", "pro-private"), "access": {"owners": ["u-owner"], "grants": {"u-bob": "full"}, "members": ["u:u-owner", "u:u-bob"]},
        })
    )
    bob = await _user_client(app, "u-bob")
    async with bob:
        body = (await bob.get("/v1/agents/ag-1")).json()
        # An unchanged reference is not re-checked: bob edits the description while the agent keeps its (private) profile.
        assert (await bob.put("/v1/agents/ag-1", json={**body, "description": "edited"})).status_code == 200
        # Adding a profile he may not use is refused; one he may use is fine.
        await _profile(app, "pro-other-private", Access())
        refused = await bob.put("/v1/agents/ag-1", json={**body, "model": {"profile_id": "pro-other-private"}})
        assert refused.status_code == 422 and refused.json()["extensions"]["error"] == "model_profile_not_found"
        assert (await bob.put("/v1/agents/ag-1", json={**body, "model": {"profile_id": "pro-public"}})).status_code == 200


async def test_a_graph_node_profile_is_checked_when_added(app):
    await _profile(app, "pro-private", Access())
    await _profile(app, "pro-public", Access(public=True))
    ada = await _user_client(app, "u-ada")
    agent = Agent.model_validate({**_agent("ag-node", "pro-public"), "access": {"public": True}})
    await app.state.storage_provider.get_storage(Agent).create(agent)

    def graph(profile_id: str) -> dict:
        return {
            "id": "gr-1", "description": "g",
            "nodes": [
                {"kind": "begin", "id": "start"},
                {"kind": "agent", "id": "a", "agent_id": "ag-node", "profile_id": profile_id},
                {"kind": "end", "id": "finish"},
            ],
            "edges": [
                {"kind": "static", "from_node": "start", "to_node": "a"},
                {"kind": "static", "from_node": "a", "to_node": "finish"},
            ],
        }

    async with ada:
        refused = await ada.post("/v1/graphs", json=graph("pro-private"))
        assert refused.status_code == 422, refused.text
        assert refused.json()["extensions"]["error"] == "model_profile_not_found"
        ok = await ada.post("/v1/graphs", json=graph("pro-public"))
        assert ok.status_code == 201, ok.text


async def test_the_tool_twin_refuses_the_same_way(app):
    from primer.api.registries import ProviderRegistry
    from primer.toolset.system import build_system_toolset

    sp = app.state.storage_provider
    await _profile(app, "pro-private", Access())
    await _profile(app, "pro-public", Access(public=True))
    toolset = build_system_toolset(storage_provider=sp, provider_registry=ProviderRegistry(sp), workspace_registry=None)
    ada = PrincipalRef(type="user", id="u-ada", display="ada", role="user", source="local", user_id="u-ada")
    ctx = ToolContext(tool_call_id="c", session_id="s", workspace_id="w", initiated_by=ada)

    refused = await toolset.call(tool_name="create_agent", arguments={"entity": _agent("ag-t1", "pro-private")}, ctx=ctx)
    assert refused.is_error and "model_profile_not_found" in refused.output or "does not exist" in refused.output
    ok = await toolset.call(tool_name="create_agent", arguments={"entity": _agent("ag-t2", "pro-public")}, ctx=ctx)
    assert not ok.is_error, ok.output
