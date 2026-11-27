"""POST/PUT /v1/model_profiles: config.extended is validated against the provider's wire at both write doors, and an
aggregated profile carries no config (PMS task 4).

The REST 422s use the router's own {error, field, message} shape (carried in the RFC 7807 extensions); the system
create_model_profile / update_model_profile tools answer the same refusals as type=validation-error. The shared check
itself is unit-driven in tests/model_profile/test_settings_checks.py.
"""

from __future__ import annotations

import pytest
from pydantic import HttpUrl, SecretStr, ValidationError

from primer.model.common import dump_for_storage
from primer.model.model_profile import ModelProfile
from primer.model.provider import (
    AnthropicConfig,
    Limits,
    LLMProvider,
    LLMProviderType,
    OpenChatConfig,
    OpenChatFlavor,
)
from tests._support.model_profiles import seed_profile
from tests.toolset.test_system_crud_guards import _call, _profile, world  # noqa: F401  (world is a fixture)


async def _seed_provider(client, provider_id: str, provider: LLMProviderType, config) -> None:
    body = dump_for_storage(
        LLMProvider(id=provider_id, provider=provider, config=config, limits=Limits(max_concurrency=4)),
    )
    r = await client.post("/v1/llm_providers", json=body)
    assert r.status_code in (200, 201), r.text


def _vllm(provider_id: str = "setv-vllm") -> LLMProvider:
    return LLMProvider(
        id=provider_id,
        provider=LLMProviderType.OPENCHAT,
        config=OpenChatConfig(
            url=HttpUrl("http://127.0.0.1:8000/v1/"),
            flavor=OpenChatFlavor.VLLM,
        ),
        limits=Limits(max_concurrency=4),
    )


class TestRestSettingsValidation:
    @pytest.mark.asyncio
    async def test_create_with_an_unknown_setting_is_422(self, client) -> None:
        await _seed_provider(
            client, "setv-anthropic", LLMProviderType.ANTHROPIC,
            AnthropicConfig(api_key=SecretStr("sk-test")),
        )
        r = await client.post(
            "/v1/model_profiles",
            json={
                "id": "setv-1",
                "description": "a profile",
                "provider_id": "setv-anthropic",
                "model_name": "m",
                "context_length": 1000,
                "config": {"extended": {"frobnicate": 1}},
            },
        )
        assert r.status_code == 422, r.text
        body = r.json()
        assert body["extensions"]["error"] == "invalid_model_setting"
        assert body["extensions"]["field"] == "config.extended.frobnicate"
        assert body["extensions"]["message"].startswith(
            "extended.frobnicate is not a setting for anthropic; accepted: "
        )

    @pytest.mark.asyncio
    async def test_update_to_a_provider_that_does_not_take_the_setting_is_422(self, client) -> None:
        """The provider change re-validates the profile's settings against the NEW wire."""
        await _seed_provider(
            client, "setv-anthropic", LLMProviderType.ANTHROPIC,
            AnthropicConfig(api_key=SecretStr("sk-test")),
        )
        await _seed_provider(client, "setv-vllm", LLMProviderType.OPENCHAT, _vllm("setv-vllm").config)
        pid = await seed_profile(
            client, "setv-anthropic", "m",
            config={"extended": {"thinking": {"type": "adaptive"}}},
        )
        r = await client.get(f"/v1/model_profiles/{pid}")
        body = r.json()
        body["provider_id"] = "setv-vllm"
        r = await client.put(f"/v1/model_profiles/{pid}", json=body)
        assert r.status_code == 422, r.text
        assert r.json()["extensions"]["error"] == "invalid_model_setting"
        assert r.json()["extensions"]["field"] == "config.extended.thinking"
        assert "is not a setting for openchat/vllm" in r.json()["extensions"]["message"]


class TestAggregatedProfileHasNoConfig:
    def test_a_non_empty_config_is_refused_at_validation(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            ModelProfile(
                id="agg-cfg-1", description="pool", kind="aggregated",
                members=["a", "b"], config={"reasoning": "low"},
            )
        assert "must not set config" in str(exc_info.value)

    def test_no_config_or_an_empty_one_is_valid(self) -> None:
        ModelProfile(
            id="agg-cfg-2", description="pool", kind="aggregated", members=["a", "b"],
        )
        ModelProfile(
            id="agg-cfg-3", description="pool", kind="aggregated",
            members=["a", "b"], config={},
        )

    @pytest.mark.asyncio
    async def test_rest_create_of_one_is_422(self, client) -> None:
        r = await client.post(
            "/v1/model_profiles",
            json={
                "id": "agg-cfg-4",
                "description": "pool",
                "kind": "aggregated",
                "members": ["a", "b"],
                "config": {"reasoning": "low"},
            },
        )
        assert r.status_code == 422, r.text
        assert "must not set config" in r.text


class TestSystemToolSettingsValidation:
    @pytest.mark.asyncio
    async def test_tool_create_with_an_unknown_setting_is_refused(self, world) -> None:
        sp, toolset, _ = world
        body = _profile("setv-tool-1").model_dump(mode="json")
        body["config"] = {"extended": {"frobnicate": 1}}
        is_error, answer = await _call(toolset, "create_model_profile", entity=body)
        assert is_error and answer["type"] == "validation-error"
        assert "config.extended.frobnicate" in answer["message"]
        assert await sp.get_storage(ModelProfile).get("setv-tool-1") is None, "a refused profile was stored"

    @pytest.mark.asyncio
    async def test_tool_update_revalidates_against_the_new_wire(self, world) -> None:
        sp, toolset, _ = world
        await sp.get_storage(LLMProvider).create(_vllm("vllm-1"))
        body = _profile("setv-tool-2").model_dump(mode="json")
        body["config"] = {"extended": {"thinking": {"type": "adaptive"}}}
        is_error, _ = await _call(toolset, "create_model_profile", entity=body)
        assert not is_error

        is_error, answer = await _call(
            toolset, "update_model_profile",
            id="setv-tool-2", entity={**body, "provider_id": "vllm-1"},
        )
        assert is_error and answer["type"] == "validation-error"
        assert "config.extended.thinking" in answer["message"]
        stored = await sp.get_storage(ModelProfile).get("setv-tool-2")
        assert stored.provider_id == "anthropic-1", "a refused update was stored"
