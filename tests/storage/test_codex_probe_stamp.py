"""Exercise the saved discovery handler against SQLite and isolated native PostgreSQL."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from primer.api.registries import ProviderRegistry
from primer.api.routers.providers import discover_saved_llm_models
from primer.model.except_ import BadRequestError
from primer.model.provider import LLMProvider
from tests.storage.test_storage_contract import provider  # noqa: F401 (SQLite and native PG)


async def _create_provider(storage):
    row = LLMProvider.model_validate({
        "id": "codex-stamp", "provider": "codex", "config": {}, "limits": {"max_concurrency": 1},
    })
    return await storage.get_storage(LLMProvider).create(row)


@pytest.mark.asyncio
async def test_saved_codex_discovery_stamps_refusal_then_success(provider):
    row = await _create_provider(provider)
    store = provider.get_storage(LLMProvider)
    models = [{"name": "account-model", "display_name": "Account model"}]
    registry = AsyncMock(spec=ProviderRegistry)
    registry.discover_codex_models.side_effect = [RuntimeError("broker-private detail"), models]

    with pytest.raises(BadRequestError, match="discovery is unavailable"):
        await discover_saved_llm_models(row.id, provider, registry)
    failed = await store.get(row.id)
    assert failed.last_probe_at is not None and failed.last_probe_ok is False
    assert "discovery is unavailable" in failed.last_error
    assert "broker-private" not in failed.last_error

    result = await discover_saved_llm_models(row.id, provider, registry)
    assert result["models"] == models
    passed = await store.get(row.id)
    assert passed.last_probe_at >= failed.last_probe_at
    assert passed.last_probe_ok is True and passed.last_error is None
    assert passed.config == row.config and passed.limits == row.limits


@pytest.mark.asyncio
@pytest.mark.parametrize("edit", ["pass_env", "timeout", "delete"])
async def test_saved_codex_probe_does_not_stamp_a_changed_or_deleted_row(provider, edit):
    row = await _create_provider(provider)
    store = provider.get_storage(LLMProvider)
    entered, release = asyncio.Event(), asyncio.Event()

    async def probe(saved):
        assert saved.id == row.id and saved.config.pass_env == []
        entered.set()
        await release.wait()
        return [{"name": "account-model", "display_name": "Account model"}]

    registry = AsyncMock(spec=ProviderRegistry)
    registry.discover_codex_models.side_effect = probe
    task = asyncio.create_task(discover_saved_llm_models(row.id, provider, registry))
    try:
        async with asyncio.timeout(5):
            await entered.wait()
            if edit == "pass_env":
                config = row.config.model_copy(update={"pass_env": ["LANG"]})
                await store.update(row.model_copy(update={"config": config}))
            elif edit == "timeout":
                limits = row.limits.model_copy(update={"total_timeout_seconds": 1.0})
                await store.update(row.model_copy(update={"limits": limits}))
            else:
                await store.delete(row.id)
            release.set()
            assert (await task)["models"][0]["name"] == "account-model"
        saved = await store.get(row.id)
        if edit == "delete":
            assert saved is None
        else:
            assert saved.last_probe_at is None and saved.last_error is None
            if edit == "pass_env":
                assert saved.config.pass_env == ["LANG"]
            else:
                assert saved.limits.total_timeout_seconds == 1.0
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
