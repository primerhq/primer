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

RULE = ('codex profiles need a direct-tool model: gpt-5.5, or a model the '
        "provider's model catalog file marks \"tool_mode\": \"direct\"")


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
async def test_profile_with_non_direct_model_is_422(client) -> None:
    await _seed_codex_provider(client, "cx-rule")
    r = await client.post("/v1/model_profiles", json=profile_body("cx-rule", "gpt-5.4-codex"))
    assert r.status_code == 422, r.text
    extensions = r.json()["extensions"]
    assert extensions["error"] == "codex_model_not_direct"
    assert extensions["field"] == "model_name"
    assert RULE in extensions["message"], extensions


@pytest.mark.asyncio
async def test_profile_with_direct_model_is_created(client) -> None:
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
async def test_update_repoint_to_codex_non_direct_is_422(client) -> None:
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
    assert extensions["error"] == "codex_model_not_direct"
    assert extensions["field"] == "model_name"
    assert RULE in extensions["message"], extensions
    # the stored row is untouched
    stored = (await client.get(f"/v1/model_profiles/{pid}")).json()
    assert stored["provider_id"] == "cx-nc-up" and stored["model_name"] == "claude-opus-4"


@pytest.mark.asyncio
async def test_catalog_direct_model_is_created(client, tmp_path) -> None:
    """The config wiring: a codex provider whose model_catalog_path file marks
    a model "tool_mode": "direct" accepts that model."""
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps({"models": [{"slug": "frontier-x", "tool_mode": "direct"}]}),
        encoding="utf-8",
    )
    await _seed_codex_provider(client, "cx-catalog", catalog=str(catalog))
    r = await client.post("/v1/model_profiles", json=profile_body("cx-catalog", "frontier-x"))
    assert r.status_code in (200, 201), r.text
