"""The codex profile rule (the codex plan, task 4; spec section 6.6): a single
profile on a codex provider must name a direct-tool model, or the create is
refused with a 422 problem that names the rule. Aggregated profiles need no
check of their own: every member is itself a single profile, checked when it
is created.
"""

from __future__ import annotations

import json

import pytest
from pydantic import SecretStr

from primer.model.common import dump_for_storage
from primer.model.provider import (
    AnthropicConfig,
    CodexConfig,
    Limits,
    LLMProvider,
    LLMProviderType,
)
from tests._support.model_profiles import profile_body

RULE = "The model is not available through this provider's authorized Codex account"


@pytest.fixture
def account_models(app, monkeypatch):
    """Trusted discovery seam only; actual signed broker IPC is covered separately."""
    names = {"gpt-5.5", "account-specific-model"}

    async def discover(provider):
        if provider.config != CodexConfig():
            raise RuntimeError("CLI overrides refused by broker")
        return [{"name": name, "display_name": name} for name in sorted(names)]

    monkeypatch.setattr(app.state.provider_registry, "discover_codex_models", discover)
    return names


async def _seed_codex_provider(client, provider_id: str, *, catalog: str | None = None) -> None:
    body = dump_for_storage(LLMProvider(
        id=provider_id,
        provider=LLMProviderType.CODEX,
        config=CodexConfig(model_catalog_path=catalog),
        limits=Limits(max_concurrency=4),
    ))
    r = await client.post("/v1/llm_providers", json=body)
    assert r.status_code in (200, 201), r.text


async def _seed_anthropic_provider(client, provider_id: str) -> None:
    body = dump_for_storage(LLMProvider(
        id=provider_id,
        provider=LLMProviderType.ANTHROPIC,
        config=AnthropicConfig(api_key=SecretStr("sk-test")),
        limits=Limits(max_concurrency=4),
    ))
    r = await client.post("/v1/llm_providers", json=body)
    assert r.status_code in (200, 201), r.text


@pytest.mark.asyncio
async def test_profile_with_non_direct_model_is_422(client, account_models) -> None:
    await _seed_codex_provider(client, "cx-rule")
    r = await client.post("/v1/model_profiles", json=profile_body("cx-rule", "gpt-5.4-codex"))
    assert r.status_code == 422, r.text
    extensions = r.json()["extensions"]
    assert extensions["error"] == "codex_model_unavailable"
    assert extensions["field"] == "model_name"
    assert RULE in extensions["message"], extensions


@pytest.mark.asyncio
async def test_profile_with_direct_model_is_created(client, account_models) -> None:
    await _seed_codex_provider(client, "cx-direct")
    r = await client.post("/v1/model_profiles", json=profile_body("cx-direct", "gpt-5.5"))
    assert r.status_code in (200, 201), r.text


@pytest.mark.asyncio
async def test_profile_on_non_codex_provider_is_created(client) -> None:
    """The check is a no-op off codex: any model on any other provider creates."""
    await _seed_anthropic_provider(client, "cx-nc")
    r = await client.post("/v1/model_profiles", json=profile_body("cx-nc", "claude-opus-4"))
    assert r.status_code in (200, 201), r.text


@pytest.mark.asyncio
async def test_update_repoint_to_codex_non_direct_is_422(client, account_models) -> None:
    """The write-door hole the review found: a profile created on a non-codex
    provider is re-pointed at the codex provider with a non-direct model on
    update, and the update is refused the same way the create is."""
    await _seed_anthropic_provider(client, "cx-nc-up")
    await _seed_codex_provider(client, "cx-rule-up")
    r0 = await client.post("/v1/model_profiles", json=profile_body("cx-nc-up", "claude-opus-4"))
    assert r0.status_code in (200, 201), r0.text
    pid = r0.json()["id"]
    body = profile_body("cx-rule-up", "gpt-5.4-codex")
    body["id"] = pid
    r = await client.put(f"/v1/model_profiles/{pid}", json=body)
    assert r.status_code == 422, r.text
    extensions = r.json()["extensions"]
    assert extensions["error"] == "codex_model_unavailable"
    assert extensions["field"] == "model_name"
    assert RULE in extensions["message"], extensions
    # the stored row is untouched
    stored = (await client.get(f"/v1/model_profiles/{pid}")).json()
    assert stored["provider_id"] == "cx-nc-up" and stored["model_name"] == "claude-opus-4"


@pytest.mark.asyncio
async def test_catalogue_cannot_authorize_a_profile(client, tmp_path, account_models) -> None:
    """An operator file cannot replace the broker account's model authorization."""
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps({"models": [{"slug": "frontier-x", "tool_mode": "direct"}]}),
        encoding="utf-8",
    )
    await _seed_codex_provider(client, "cx-catalog", catalog=str(catalog))
    r = await client.post("/v1/model_profiles", json=profile_body("cx-catalog", "frontier-x"))
    assert r.status_code == 422, r.text
    assert r.json()["extensions"]["error"] == "codex_discovery_unavailable"


@pytest.mark.asyncio
async def test_saved_discovery_and_profile_update_share_current_account(client, account_models):
    await _seed_codex_provider(client, "cx-discovery")
    discovered = await client.get("/v1/llm_providers/cx-discovery/discovered_models")
    assert discovered.status_code == 200, discovered.text
    body = discovered.json()
    assert body == {
        "models": [{"name": name, "display_name": name} for name in sorted(account_models)],
        "source": "account_policy_filtered_broker", "transport": "model_only_responses",
    }
    # No model-specific context or capability metadata was fabricated.
    created = await client.post("/v1/model_profiles", json=profile_body("cx-discovery", "account-specific-model"))
    assert created.status_code in (200, 201), created.text
    original = created.json()
    account_models.remove("account-specific-model")
    updated = await client.put(f'/v1/model_profiles/{original["id"]}', json=original)
    assert updated.status_code == 422, updated.text
    assert updated.json()["extensions"]["error"] == "codex_model_unavailable"
    stored = (await client.get(f'/v1/model_profiles/{original["id"]}')).json()
    assert stored["model_name"] == "account-specific-model"


@pytest.mark.asyncio
async def test_missing_binding_and_draft_have_no_model_fallback(client):
    await _seed_codex_provider(client, "cx-no-binding")
    created = await client.post("/v1/model_profiles", json=profile_body("cx-no-binding", "gpt-5.5"))
    assert created.status_code == 422, created.text
    assert created.json()["extensions"]["error"] == "codex_discovery_unavailable"
    saved = await client.get("/v1/llm_providers/cx-no-binding/discovered_models")
    assert saved.status_code == 400, saved.text
    draft = await client.post("/v1/llm_providers/_discover_models", json={"provider": "codex", "config": {}})
    assert draft.status_code == 400, draft.text
    assert "saved provider" in draft.text


@pytest.mark.asyncio
async def test_system_tool_discovery_and_profile_writes_use_same_registry(client, app, account_models):
    from tests._support.caller import ADMIN_CALLER

    await _seed_codex_provider(client, "cx-tool")
    toolset = app.state.system_toolset
    discovered = await toolset.call(tool_name="fetch_llm_provider_models",
                                    arguments={"provider_id": "cx-tool"}, ctx=ADMIN_CALLER)
    assert not discovered.is_error, discovered.output
    result = json.loads(discovered.output)
    assert {m["name"] for m in result["models"]} == account_models
    body = profile_body("cx-tool", "account-specific-model")
    created = await toolset.call(tool_name="create_model_profile", arguments={"entity": body}, ctx=ADMIN_CALLER)
    assert not created.is_error, created.output
    profile = json.loads(created.output)
    account_models.clear()
    refused = await toolset.call(tool_name="update_model_profile",
                                 arguments={"id": profile["id"], "entity": profile}, ctx=ADMIN_CALLER)
    assert refused.is_error
    assert json.loads(refused.output)["type"] == "validation-error"
    stored = (await client.get(f'/v1/model_profiles/{profile["id"]}')).json()
    assert stored["model_name"] == "account-specific-model"


@pytest.mark.asyncio
async def test_discovery_stamp_does_not_cross_codex_config_change(client, app, monkeypatch):
    await _seed_codex_provider(client, "cx-stamp")

    async def discover(provider):
        assert provider.config == CodexConfig()
        current = (await client.get("/v1/llm_providers/cx-stamp")).json()
        current["config"]["codex_home"] = "/operator/changed-home"
        changed = await client.put("/v1/llm_providers/cx-stamp", json=current)
        assert changed.status_code == 200, changed.text
        return [{"name": "model", "display_name": "Model"}]

    monkeypatch.setattr(app.state.provider_registry, "discover_codex_models", discover)
    discovered = await client.get("/v1/llm_providers/cx-stamp/discovered_models")
    assert discovered.status_code == 200, discovered.text
    current = (await client.get("/v1/llm_providers/cx-stamp")).json()
    assert current["config"]["codex_home"] == "/operator/changed-home"
    assert current["last_probe_at"] is None
