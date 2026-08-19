"""S5 P3: explicit seeding and the operator/builder reset affordance."""
from __future__ import annotations

from primer.bootstrap.defaults import (
    RESERVED_BUILDER_AGENT,
    RESERVED_OPERATOR_AGENT,
)
from primer.bootstrap.operator_defaults import (
    OPERATOR_PROMPT,
    OPERATOR_TOOLS,
)
from primer.model.agent import Agent


async def test_seed_is_idempotent_and_reports_the_setup_state(client, app):
    r = await client.post("/v1/setup/seed")
    assert r.status_code == 200
    body = r.json()
    assert body["errors"] == []
    assert "setup_complete" in body and "setup_missing" in body
    again = await client.post("/v1/setup/seed")
    assert again.json()["created"] == []


async def test_reset_agents_restores_prompt_and_grants(client, app):
    agents = app.state.storage_provider.get_storage(Agent)
    operator = await agents.get(RESERVED_OPERATOR_AGENT)
    original_profile = operator.model.profile_id
    operator.system_prompt = ["wrecked"]
    operator.tools = []
    operator.description = "wrecked"
    await agents.update(operator)

    r = await client.post("/v1/setup/reset_agents")
    assert r.status_code == 200
    assert r.json()["reset"] == [RESERVED_OPERATOR_AGENT, RESERVED_BUILDER_AGENT]

    restored = await agents.get(RESERVED_OPERATOR_AGENT)
    assert restored.system_prompt == list(OPERATOR_PROMPT)
    assert restored.tools == list(OPERATOR_TOOLS)
    assert restored.description != "wrecked"
    assert restored.model.profile_id == original_profile


async def test_reset_agents_touches_nothing_else(client, app):
    agents = app.state.storage_provider.get_storage(Agent)
    from primer.model.agent import AgentModel

    await agents.create(
        Agent(
            id="agent-mine",
            description="mine",
            model=AgentModel(profile_id="prov--m"),
            system_prompt=["mine"],
        )
    )
    await client.post("/v1/setup/reset_agents")
    mine = await agents.get("agent-mine")
    assert mine.system_prompt == ["mine"] and mine.description == "mine"


async def test_setup_endpoints_are_admin_only(raw_client, app):
    """A role='user' account is rejected on both endpoints.

    There is no non-admin client fixture; this is the construction
    tests/api/test_rbac_router_wiring.py:20-53 uses, verbatim in shape:
    register the first account (which becomes the admin), seed a second
    row with role='user', then log in as it on the same client so the
    cookie is replaced.
    """
    from datetime import datetime, timezone

    from primer.auth.passwords import hash_password
    from primer.model.user import User

    r = await raw_client.post(
        "/v1/auth/register",
        json={"username": "testuser", "password": "testpassword"},
    )
    assert r.status_code == 200, r.text
    await app.state.storage_provider.get_storage(User).create(
        User(
            id="user-plain",
            username="plain",
            password_hash=await hash_password("pw-plain-pw"),
            created_at=datetime.now(timezone.utc),
            role="user",
        )
    )
    login = await raw_client.post(
        "/v1/auth/login", json={"username": "plain", "password": "pw-plain-pw"},
    )
    assert login.status_code == 200, login.text

    for path in ("/v1/setup/seed", "/v1/setup/reset_agents"):
        resp = await raw_client.post(path)
        assert resp.status_code == 403, (path, resp.text)
        assert resp.json()["extensions"]["error"] == "forbidden_role"
