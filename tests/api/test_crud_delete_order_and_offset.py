"""GS-23: the row is deleted before the on_delete hook runs. GS-24: an offset past a signed 64-bit integer is a 422."""

from __future__ import annotations

from primer.model.provider import LLMProvider
from primer.model.common import dump_for_storage
from tests.api.test_providers import _llm


async def test_the_row_is_gone_when_the_delete_hook_runs(client, app) -> None:
    await client.post("/v1/llm_providers", json=dump_for_storage(_llm()))
    for profile in (await client.get("/v1/model_profiles?limit=200")).json()["items"]:
        await client.delete(f"/v1/model_profiles/{profile['id']}")
    registry = app.state.provider_registry
    seen: dict[str, object] = {}
    real = registry.invalidate_llm

    async def spy(provider_id):
        seen["row_at_hook"] = await app.state.storage_provider.get_storage(LLMProvider).get(provider_id)
        await real(provider_id)

    registry.invalidate_llm = spy
    try:
        assert (await client.delete("/v1/llm_providers/anthropic-1")).status_code == 204
    finally:
        del registry.invalidate_llm
    assert "row_at_hook" in seen and seen["row_at_hook"] is None


async def test_an_offset_past_a_signed_64_bit_integer_is_a_422(client) -> None:
    assert (await client.get("/v1/agents?offset=99999999999999999999")).status_code == 422
    assert (await client.get("/v1/agents?offset=0")).status_code == 200
