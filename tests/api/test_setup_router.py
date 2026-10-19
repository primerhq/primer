"""S5 P3: explicit seeding and the operator/builder reset affordance."""
from __future__ import annotations

from datetime import datetime, timezone

from primer.bootstrap.defaults import (
    RESERVED_EXPLORER_AGENT,
    RESERVED_PLANNER_AGENT,
    RESERVED_TOOL_RUNNER_AGENT,
    RESERVED_BUILDER_AGENT,
    RESERVED_OPERATOR_AGENT,
)
from primer.bootstrap.operator_defaults import (
    OPERATOR_PROMPT,
    OPERATOR_TOOLS,
)
from primer.model.agent import Agent
from primer.model.workspace import Workspace, WorkspaceRuntimeMeta, WorkspaceTemplate


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
    # Phase 1 widened the reset to the whole base roster.
    assert r.json()["reset"] == [
        RESERVED_OPERATOR_AGENT,
        RESERVED_BUILDER_AGENT,
        RESERVED_PLANNER_AGENT,
        RESERVED_EXPLORER_AGENT,
        RESERVED_TOOL_RUNNER_AGENT,
    ]

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


class _LiveWorkspace:
    def __init__(self, workspace_id):
        self.id = workspace_id
        self.runtime_meta = WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959", token="stub-token")


class _RecordingRegistry:
    """Materialises anything and notes which template and id it was asked for."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str | None]] = []

    async def materialise(self, *, template, overrides=None, workspace_id=None):
        self.seen.append((template.id, workspace_id))
        return _LiveWorkspace(workspace_id)


async def _template(app, template_id: str, provider_id: str) -> None:
    await app.state.storage_provider.get_storage(WorkspaceTemplate).create(
        WorkspaceTemplate(id=template_id, description=template_id, provider_id=provider_id, backend={"kind": "local"})
    )


async def test_seed_uses_the_configured_default_template(client, app):
    """The route hands the ensure pass the deployment's ``default_workspace_template``.

    It used to pass nothing, so the pass fell back to ``local-default`` whatever the config said, and a Kubernetes
    deployment's re-run seed (the wizard's last step) put its default workspace on the API pod's own disk.
    """
    sp = app.state.storage_provider
    await _template(app, "local-default", "local")
    await _template(app, "cluster-default", "k3s")
    registry = _RecordingRegistry()
    app.state.workspace_registry = registry
    app.state.config.default_workspace_template = "cluster-default"

    r = await client.post("/v1/setup/seed")

    assert r.status_code == 200, r.text
    assert "prime" in r.json()["created"]
    row = await sp.get_storage(Workspace).get("prime")
    assert (row.template_id, row.provider_id, row.name) == ("cluster-default", "k3s", "prime")
    assert registry.seen == [("cluster-default", "prime")]
    assert (await sp.get_system_state()).default_workspace_id == "prime"


async def test_seed_without_a_configured_template_uses_the_reserved_local_one(client, app):
    await _template(app, "local-default", "local")
    registry = _RecordingRegistry()
    app.state.workspace_registry = registry
    r = await client.post("/v1/setup/seed")
    assert r.status_code == 200, r.text
    assert registry.seen == [("local-default", "prime")]


async def test_seed_defers_the_default_workspace_when_the_configured_template_is_absent(client, app):
    """A typo'd template must not silently seed onto local instead: nothing is created and nothing is recorded."""
    await _template(app, "local-default", "local")
    registry = _RecordingRegistry()
    app.state.workspace_registry = registry
    app.state.config.default_workspace_template = "no-such-template"
    r = await client.post("/v1/setup/seed")
    assert r.status_code == 200, r.text
    assert "prime" not in r.json()["created"]
    assert registry.seen == []
    assert "default_workspace" in r.json()["setup_missing"]
    assert (await app.state.storage_provider.get_system_state()).default_workspace_id is None


async def test_seed_adopts_a_legacy_primer_workspace(client, app):
    """An install that predates the record keeps its ``primer`` workspace: the re-run seed adopts it and creates nothing."""
    sp = app.state.storage_provider
    await _template(app, "local-default", "local")
    registry = _RecordingRegistry()
    app.state.workspace_registry = registry
    await sp.get_storage(Workspace).create(Workspace(
        id="primer", name="primer", template_id="local-default", provider_id="local",
        created_at=datetime.now(timezone.utc),
        runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959", token="t"),
    ))
    r = await client.post("/v1/setup/seed")
    assert r.status_code == 200, r.text
    assert registry.seen == []
    assert (await sp.get_storage(Workspace).get("primer")).name == "prime"
    assert await sp.get_storage(Workspace).get("prime") is None
    assert (await sp.get_system_state()).default_workspace_id == "primer"
    assert "default_workspace" not in r.json()["setup_missing"]
