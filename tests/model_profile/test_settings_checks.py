"""check_profile_settings: a profile's config.extended is validated against its provider's wire on both write doors (PMS task 4).

The REST router and the system tools share these checks; this file unit-drives the shared functions over a
two-row storage so the message forms (spec section 6 and the plan's Global Constraints) are pinned where
they are raised. The door wiring itself (the router's _on_pre_create / _on_pre_update and the tools'
pre-create / pre-update hooks) is pinned in tests/api/test_model_profile_settings_validation.py.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import HttpUrl, SecretStr

from primer.common.entity_checks import EntityCheckError
from primer.model.model_profile import ModelProfile, ModelProfileConfig
from primer.model.provider import (
    AnthropicConfig,
    Limits,
    LLMProvider,
    LLMProviderType,
    OpenChatConfig,
    OpenChatFlavor,
)
from primer.model_profile.checks import (
    check_profile_on_create,
    check_profile_on_update,
    check_profile_settings,
)


class _Storage:
    def __init__(self, rows: dict[str, Any] | None = None) -> None:
        self._rows = dict(rows or {})

    async def get(self, entity_id: str) -> Any:
        return self._rows.get(entity_id)


class _StorageProvider:
    def __init__(self, providers: dict[str, Any] | None = None) -> None:
        self._providers = _Storage(providers)
        self._profiles = _Storage()

    def get_storage(self, model: Any) -> Any:
        if model is LLMProvider:
            return self._providers
        if model is ModelProfile:
            return self._profiles
        raise AssertionError(f"unexpected storage request for {model}")


def _anthropic_provider(provider_id: str = "prov-anthropic") -> LLMProvider:
    return LLMProvider(
        id=provider_id,
        provider=LLMProviderType.ANTHROPIC,
        config=AnthropicConfig(api_key=SecretStr("sk-test")),
        limits=Limits(max_concurrency=4),
    )


def _vllm_provider(provider_id: str = "prov-vllm") -> LLMProvider:
    return LLMProvider(
        id=provider_id,
        provider=LLMProviderType.OPENCHAT,
        config=OpenChatConfig(
            url=HttpUrl("http://127.0.0.1:8000/v1/"),
            flavor=OpenChatFlavor.VLLM,
        ),
        limits=Limits(max_concurrency=4),
    )


def _profile(
    provider_id: str = "prov-anthropic",
    config: ModelProfileConfig | None = None,
    **fields: Any,
) -> ModelProfile:
    base = dict(
        id="mp-1",
        description="a profile",
        kind="single",
        provider_id=provider_id,
        model_name="m",
        context_length=1000,
    )
    base.update(fields)
    return ModelProfile(**base, config=config or ModelProfileConfig())


class TestCheckProfileSettings:
    async def test_unknown_key_is_refused_naming_the_wire_and_its_keys(self) -> None:
        sp = _StorageProvider({"prov-anthropic": _anthropic_provider()})
        entity = _profile(config=ModelProfileConfig(extended={"frobnicate": 1}))
        with pytest.raises(EntityCheckError) as exc_info:
            await check_profile_settings(entity, storage_provider=sp)
        exc = exc_info.value
        assert exc.code == "invalid_model_setting"
        assert exc.field == "config.extended.frobnicate"
        assert exc.message.startswith(
            "extended.frobnicate is not a setting for anthropic; accepted: "
        )

    async def test_bad_value_is_refused_naming_the_path(self) -> None:
        # The anthropic wire's top_k is an integer; "wide" is not one.
        sp = _StorageProvider({"prov-anthropic": _anthropic_provider()})
        entity = _profile(config=ModelProfileConfig(extended={"top_k": "wide"}))
        with pytest.raises(EntityCheckError) as exc_info:
            await check_profile_settings(entity, storage_provider=sp)
        exc = exc_info.value
        assert exc.code == "invalid_model_setting"
        assert exc.field == "config.extended.top_k"
        assert exc.message == "extended.top_k: expected an integer"

    async def test_reserved_body_field_is_refused(self) -> None:
        sp = _StorageProvider({"prov-vllm": _vllm_provider()})
        entity = _profile(
            provider_id="prov-vllm",
            config=ModelProfileConfig(extended={"body": {"model": "evil"}}),
        )
        with pytest.raises(EntityCheckError) as exc_info:
            await check_profile_settings(entity, storage_provider=sp)
        exc = exc_info.value
        assert exc.code == "invalid_model_setting"
        assert exc.field == "config.extended.body.model"
        assert exc.message == "extended.body.model: reserved field, primer sets it itself"

    async def test_multiple_problems_join_into_one_refusal(self) -> None:
        # One refusal per save: the first path in the field, every problem's message in the text.
        sp = _StorageProvider({"prov-anthropic": _anthropic_provider()})
        entity = _profile(config=ModelProfileConfig(extended={"frobnicate": 1, "top_k": "wide"}))
        with pytest.raises(EntityCheckError) as exc_info:
            await check_profile_settings(entity, storage_provider=sp)
        exc = exc_info.value
        assert exc.field == "config.extended.frobnicate"
        assert "extended.frobnicate is not a setting for anthropic" in exc.message
        assert "extended.top_k: expected an integer" in exc.message
        # One join separator plus the unknown-key message's own "; accepted:" separator.
        assert exc.message.count(";") == 2

    async def test_a_valid_profile_passes(self) -> None:
        # vLLM takes top_k and passes the body through; a reserved-free body field is fine.
        sp = _StorageProvider({"prov-vllm": _vllm_provider()})
        entity = _profile(
            provider_id="prov-vllm",
            config=ModelProfileConfig(
                extended={"top_k": 5, "body": {"return_progress": True}},
            ),
        )
        await check_profile_settings(entity, storage_provider=sp)
        # An empty config has nothing to check at all.
        await check_profile_settings(
            _profile(), storage_provider=_StorageProvider({"prov-anthropic": _anthropic_provider()})
        )

    async def test_aggregated_profiles_carry_no_settings(self) -> None:
        # The model refuses a config on an aggregated profile, so there is
        # nothing for the check to validate: no provider lookup, no refusal.
        entity = ModelProfile(
            id="agg-1", description="pool", kind="aggregated", members=["a", "b"],
        )
        await check_profile_settings(entity, storage_provider=_StorageProvider())


class TestProviderChangeRevalidates:
    async def test_provider_change_revalidates(self) -> None:
        """A profile valid on its provider is re-validated against the NEW wire when
        its provider changes: the check runs on the whole profile."""
        sp = _StorageProvider(
            {"prov-anthropic": _anthropic_provider(), "prov-vllm": _vllm_provider()},
        )
        entity = _profile(
            config=ModelProfileConfig(
                extended={"thinking": {"type": "adaptive"}},
            ),
        )
        await check_profile_on_create(entity, storage_provider=sp)  # valid on anthropic
        moved = entity.model_copy(update={"provider_id": "prov-vllm"})
        with pytest.raises(EntityCheckError) as exc_info:
            await check_profile_on_update(moved, entity, storage_provider=sp)
        exc = exc_info.value
        assert exc.code == "invalid_model_setting"
        assert exc.field == "config.extended.thinking"
        assert "is not a setting for openchat/vllm" in exc.message
