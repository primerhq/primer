"""GET /v1/setup/state - the six live setup predicates (R5, uiv2 notes
section 4/5; docs/superpowers/uiv2/03-backend-gap-map.md:162,
BACKEND-GAP #13: 2 of 6 predicates were presence-only, not live).
"""
from __future__ import annotations

from primer.model.provider import (
    AnthropicConfig,
    LLMProvider,
    LLMProviderType,
)
from primer.model.providers._shared import Limits
from primer.model.workspace import Workspace, WorkspaceRuntimeMeta


_ALL_KEYS = {
    "llm_provider", "model_profile", "default_workspace",
    "operator_agent", "builder_agent", "system_collection",
}


async def test_state_returns_six_predicates_default_fixture(client, app):
    r = await client.get("/v1/setup/state")
    assert r.status_code == 200, r.text
    body = r.json()
    by_key = {p["key"]: p for p in body["predicates"]}
    assert set(by_key) == _ALL_KEYS

    # Seeded by run_ensure_pass in the app fixture (parity with the
    # production lifespan) - these three are presence-ok. The fake test
    # storage_provider's workspace materialisation is not part of this
    # fixture's ensure-pass (no backend to actually attach to), so
    # default_workspace stays genuinely missing here too - covered below.
    assert by_key["model_profile"]["ok"] is True
    assert by_key["operator_agent"] == {
        "key": "operator_agent", "label": "Operator agent seeded",
        "ok": True, "live": False, "detail": None,
    }
    assert by_key["builder_agent"]["ok"] is True
    assert by_key["builder_agent"]["live"] is False
    assert by_key["system_collection"]["ok"] is True
    assert by_key["system_collection"]["live"] is False

    # Not seeded by the fixture - no LLM provider, no workspace row.
    assert by_key["llm_provider"] == {
        "key": "llm_provider", "label": "LLM provider responds",
        "ok": False, "live": False, "detail": "no LLM provider configured",
    }
    assert by_key["default_workspace"] == {
        "key": "default_workspace", "label": "Default workspace reachable",
        "ok": False, "live": False,
        "detail": "default workspace row does not exist",
    }

    assert body["complete"] is False
    assert set(body["missing"]) == {"llm_provider", "default_workspace"}


async def test_llm_provider_live_check_fails_on_unreachable_provider(client, app):
    """A configured-but-unreachable provider (no real credentials/network
    in this sandbox) is the actual "responds" liveness signal the
    presence-only check could never catch. Anthropic (api_key-only, no
    url field) rather than Ollama/OpenAI-compatible: those pass an
    HttpUrl straight into the ollama SDK / httpx without stringifying
    it first (providers.py's _probe_ollama_models /
    _probe_openai_compatible_models), a pre-existing bug outside this
    file's scope - reported separately, worked around here rather than
    silently depending on it."""
    providers = app.state.storage_provider.get_storage(LLMProvider)
    await providers.create(LLMProvider(
        id="llm-provider-dead-test",
        provider=LLMProviderType.ANTHROPIC,
        config=AnthropicConfig(api_key="sk-not-a-real-key"),
        limits=Limits(max_concurrency=1),
    ))
    r = await client.get("/v1/setup/state")
    assert r.status_code == 200, r.text
    by_key = {p["key"]: p for p in r.json()["predicates"]}
    assert by_key["llm_provider"]["ok"] is False
    assert by_key["llm_provider"]["live"] is True
    assert by_key["llm_provider"]["detail"]


async def test_default_workspace_live_check_fails_on_unresolvable_row(client, app):
    """A workspace row exists but points at a provider_id no backend is
    registered under - the live re-attach must fail (and be REPORTED as
    failing), not silently pass just because the row is present. This is
    exactly the presence-vs-liveness gap the predicate upgrade closes."""
    from datetime import datetime, timezone

    storage = app.state.storage_provider.get_storage(Workspace)
    await app.state.storage_provider.set_default_workspace_id("prime")
    await storage.create(Workspace(
        id="prime",
        template_id="does-not-exist-template",
        provider_id="does-not-exist-provider",
        created_at=datetime.now(timezone.utc),
        runtime_meta=WorkspaceRuntimeMeta(url="ws://nowhere", token="nope"),
    ))

    r = await client.get("/v1/setup/state")
    assert r.status_code == 200, r.text
    by_key = {p["key"]: p for p in r.json()["predicates"]}
    assert by_key["default_workspace"]["ok"] is False
    assert by_key["default_workspace"]["live"] is True
    assert by_key["default_workspace"]["detail"]


async def test_default_workspace_detail_is_redacted_at_state_construction(
    client, app,
):
    class _LeakyRegistry:
        async def get_workspace(self, workspace_id: str):
            raise RuntimeError(
                "attach https://host/v1?access_token=tok-secret "
                "with Bearer sk-live-secret"
            )

    app.state.workspace_registry = _LeakyRegistry()
    await _seed_workspace_row(app, "prime")
    await app.state.storage_provider.set_default_workspace_id("prime")

    predicate = await _default_workspace_predicate(client)
    assert predicate["ok"] is False and predicate["live"] is True
    assert "tok-secret" not in predicate["detail"]
    assert "sk-live-secret" not in predicate["detail"]
    assert predicate["detail"].count("[REDACTED]") == 2


async def test_setup_state_endpoint_is_admin_only(raw_client, app):
    """Mirrors test_setup_router.py's test_setup_endpoints_are_admin_only
    for the two REUSE endpoints - same construction, extended to /state."""
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

    resp = await raw_client.get("/v1/setup/state")
    assert resp.status_code == 403, resp.text
    assert resp.json()["extensions"]["error"] == "forbidden_role"


class _ReachableRegistry:
    """A workspace registry that can attach any workspace and notes which ids it was asked for."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def get_workspace(self, workspace_id: str):
        self.asked.append(workspace_id)
        return object()


async def _default_workspace_predicate(client) -> dict:
    r = await client.get("/v1/setup/state")
    assert r.status_code == 200, r.text
    return {p["key"]: p for p in r.json()["predicates"]}["default_workspace"]


async def _seed_workspace_row(app, workspace_id: str) -> None:
    from datetime import datetime, timezone

    await app.state.storage_provider.get_storage(Workspace).create(Workspace(
        id=workspace_id,
        template_id="local-default",
        provider_id="local",
        created_at=datetime.now(timezone.utc),
        runtime_meta=WorkspaceRuntimeMeta(url="ws://nowhere", token="nope"),
    ))


async def test_default_workspace_live_check_probes_the_recorded_id(client, app):
    """A fresh install records ``prime``; the re-attach asks for that id, not for a constant."""
    registry = _ReachableRegistry()
    app.state.workspace_registry = registry
    await _seed_workspace_row(app, "prime")
    await app.state.storage_provider.set_default_workspace_id("prime")
    predicate = await _default_workspace_predicate(client)
    assert (predicate["ok"], predicate["live"], predicate["detail"]) == (True, True, None)
    assert registry.asked == ["prime"]


async def test_default_workspace_live_check_probes_the_legacy_row_on_an_unrecorded_install(client, app):
    registry = _ReachableRegistry()
    app.state.workspace_registry = registry
    await _seed_workspace_row(app, "primer")
    predicate = await _default_workspace_predicate(client)
    assert (predicate["ok"], predicate["live"]) == (True, True)
    assert registry.asked == ["primer"]


async def test_default_workspace_live_check_without_a_default_says_none_is_recorded(app):
    from types import SimpleNamespace

    from primer.api.routers.setup import _live_default_workspace_check

    registry = _ReachableRegistry()
    app.state.workspace_registry = registry
    request = SimpleNamespace(app=app)
    assert await _live_default_workspace_check(request) == (False, "no default workspace recorded")
    assert registry.asked == [], "nothing is probed when there is no default"
