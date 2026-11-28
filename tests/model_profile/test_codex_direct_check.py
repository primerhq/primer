"""The codex direct-model check called straight from ``primer.model_profile.checks``.

The system tools ``system__create_model_profile`` / ``system__update_model_profile``
call ``check_profile_on_create`` / ``check_profile_on_update`` directly (the
rest of the write doors go through the same two composites), and none of them
ever reaches the REST router's hooks, so the refusal must fire here as an
:class:`~primer.common.entity_checks.EntityCheckError` with code
``codex_model_unavailable`` when authenticated discovery does not offer the
model. No model name or catalogue hint substitutes for account discovery.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from primer.common.entity_checks import EntityCheckError
from primer.model.model_profile import ModelProfile
from primer.model.provider import AnthropicConfig, CodexConfig, Limits, LLMProvider, LLMProviderType
from primer.model_profile.checks import check_profile_on_create, check_profile_on_update
from tests._support.model_profiles import profile_id_for


class _Rows:
    """A storage stand-in that answers ``get`` with the one row the checks ask for."""

    def __init__(self, rows: dict[str, LLMProvider]) -> None:
        self._rows = rows

    async def get(self, key: str) -> LLMProvider | None:
        return self._rows.get(key)


class _StorageProvider:
    def __init__(self, llm_rows: dict[str, LLMProvider]) -> None:
        self._llm = _Rows(llm_rows)

    def get_storage(self, model):
        if model is LLMProvider:
            return self._llm
        raise AssertionError(f"unexpected storage ask for {model!r}")


def _codex_provider(provider_id: str) -> LLMProvider:
    return LLMProvider(
        id=provider_id,
        provider=LLMProviderType.CODEX,
        config=CodexConfig(),
        limits=Limits(max_concurrency=4),
    )


def _single(profile_id: str, provider_id: str, model_name: str) -> ModelProfile:
    return ModelProfile(
        id=profile_id,
        description=f"{model_name} on {provider_id}",
        provider_id=provider_id,
        model_name=model_name,
        context_length=32_768,
        config={},
    )


class _Discovery:
    def __init__(self, names=()):
        self.names = names
        self.asked = []

    async def discover_codex_models(self, provider):
        self.asked.append(provider.id)
        return [{"name": name, "display_name": name} for name in self.names]


@pytest.mark.asyncio
async def test_check_profile_on_create_refuses_non_direct_codex() -> None:
    storage_provider = _StorageProvider({"cx-check": _codex_provider("cx-check")})
    entity = _single(
        profile_id_for("cx-check", "gpt-5.4-codex"), "cx-check", "gpt-5.4-codex",
    )
    with pytest.raises(EntityCheckError) as exc_info:
        await check_profile_on_create(entity, storage_provider=storage_provider, provider_registry=_Discovery())
    assert exc_info.value.code == "codex_model_unavailable"
    assert exc_info.value.field == "model_name"


@pytest.mark.asyncio
async def test_check_profile_on_update_refuses_non_direct_codex() -> None:
    storage_provider = _StorageProvider({"cx-check": _codex_provider("cx-check")})
    profile_id = profile_id_for("cx-check", "gpt-5.5")
    existing = _single(profile_id, "cx-check", "gpt-5.5")
    entity = _single(profile_id, "cx-check", "gpt-5.4-codex")
    with pytest.raises(EntityCheckError) as exc_info:
        await check_profile_on_update(entity, existing, storage_provider=storage_provider, provider_registry=_Discovery())
    assert exc_info.value.code == "codex_model_unavailable"
    assert exc_info.value.field == "model_name"


@pytest.mark.asyncio
async def test_check_profile_on_create_allows_direct_codex() -> None:
    """Availability is taken from the exact provider's discovery, not a name allowlist."""
    storage_provider = _StorageProvider({"cx-check": _codex_provider("cx-check")})
    profile_id = profile_id_for("cx-check", "account-model")
    entity = _single(profile_id, "cx-check", "account-model")
    discovery = _Discovery(["account-model"])
    await check_profile_on_create(entity, storage_provider=storage_provider, provider_registry=discovery)
    await check_profile_on_update(entity, entity, storage_provider=storage_provider, provider_registry=discovery)
    assert discovery.asked == ["cx-check", "cx-check"]
    discovery.names = []
    with pytest.raises(EntityCheckError, match="not available"):
        await check_profile_on_update(entity, entity, storage_provider=storage_provider, provider_registry=discovery)


@pytest.mark.asyncio
async def test_name_or_catalogue_cannot_replace_missing_authority(tmp_path):
    provider = _codex_provider("cx-check")
    catalog = tmp_path / "catalog"
    catalog.write_text('[{"slug":"gpt-5.5","tool_mode":"direct"}]')
    provider.config.model_catalog_path = str(catalog)
    storage = _StorageProvider({provider.id: provider})
    entity = _single("profile", provider.id, "gpt-5.5")
    with pytest.raises(EntityCheckError) as caught:
        await check_profile_on_create(entity, storage_provider=storage)
    assert caught.value.code == "codex_discovery_unavailable"


@pytest.mark.asyncio
async def test_discovery_cancellation_propagates_and_error_is_sanitized():
    import asyncio

    entity = _single("profile", "cx-check", "gpt-5.5")
    storage = _StorageProvider({"cx-check": _codex_provider("cx-check")})

    class Failure:
        error = RuntimeError("private trust path and account detail")

        async def discover_codex_models(self, provider):
            raise self.error

    discovery = Failure()
    with pytest.raises(EntityCheckError) as caught:
        await check_profile_on_create(entity, storage_provider=storage, provider_registry=discovery)
    assert "private" not in str(caught.value)
    discovery.error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await check_profile_on_create(entity, storage_provider=storage, provider_registry=discovery)
