"""GET /v1/setup/state - the six live setup predicates (R5, uiv2 notes
section 4/5; docs/superpowers/uiv2/03-backend-gap-map.md:162,
BACKEND-GAP #13: 2 of 6 predicates were presence-only, not live).
"""
from __future__ import annotations

from primer.bootstrap.defaults import RESERVED_DEFAULT_WORKSPACE
from primer.model.provider import (
    AggregatedLLMConfig,
    AggregatedMember,
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


async def test_llm_provider_live_check_falls_back_to_pass_when_probe_unsupported(
    client, app,
):
    """Aggregated providers have no list-models probe (_probe_llm_models'
    own dispatch has no branch for them) - no live signal either way, so
    this must read as configured rather than a false failure that would
    block the wizard gate for a legitimately-working setup."""
    providers = app.state.storage_provider.get_storage(LLMProvider)
    await providers.create(LLMProvider(
        id="llm-provider-agg-test",
        provider=LLMProviderType.AGGREGATED,
        config=AggregatedLLMConfig(members=[
            AggregatedMember(provider_id="does-not-exist", model_name="whatever"),
        ]),
        limits=Limits(max_concurrency=1),
    ))
    r = await client.get("/v1/setup/state")
    assert r.status_code == 200, r.text
    by_key = {p["key"]: p for p in r.json()["predicates"]}
    assert by_key["llm_provider"] == {
        "key": "llm_provider", "label": "LLM provider responds",
        "ok": True, "live": True, "detail": None,
    }


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
    await storage.create(Workspace(
        id=RESERVED_DEFAULT_WORKSPACE,
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
