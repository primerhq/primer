"""Form metadata for the three model-family provider classes.

The console used to own this table (ui/components/providers.jsx:53-239,
"single source of truth mirroring the backend's provider enums"), and it
had drifted: it offered openresponses flavors ["openai", "lmstudio",
"other"] while OpenResponsesFlavor has carried VLLM all along. The enums
live in the backend, so the field shape is served from the backend.
"""

from __future__ import annotations

from typing import Any

import pytest

from primer.model.provider import (
    CrossEncoderProviderType,
    EmbeddingProviderType,
    LLMProviderType,
    OpenAIEmbeddingFlavor,
    OpenChatFlavor,
    OpenResponsesFlavor,
)


def _keys(fields: list[dict[str, Any]]) -> list[str]:
    return [f["key"] for f in fields]


def _field(fields: list[dict[str, Any]], key: str) -> dict[str, Any]:
    return next(f for f in fields if f["key"] == key)


@pytest.mark.asyncio
async def test_llm_types_cover_every_enum_member(client) -> None:
    r = await client.get("/v1/llm_providers/_types")
    assert r.status_code == 200, r.text
    assert set(r.json()) == {t.value for t in LLMProviderType}


@pytest.mark.asyncio
async def test_openchat_describes_its_connection_fields(client) -> None:
    body = (await client.get("/v1/llm_providers/_types")).json()
    fields = body[LLMProviderType.OPENCHAT.value]["config_fields"]
    assert _keys(fields) == ["url", "api_key", "flavor"]
    assert _field(fields, "url")["type"] == "url"
    assert _field(fields, "url")["required"] is True
    # A key box that echoes the secret back is a leak waiting to happen.
    assert _field(fields, "api_key")["type"] == "password"
    assert _field(fields, "api_key")["required"] is False


@pytest.mark.asyncio
async def test_flavor_options_come_from_the_enums(client) -> None:
    body = (await client.get("/v1/llm_providers/_types")).json()
    resp = body[LLMProviderType.OPENRESPONSES.value]["config_fields"]
    chat = body[LLMProviderType.OPENCHAT.value]["config_fields"]
    assert _field(resp, "flavor")["options"] == [
        f.value for f in OpenResponsesFlavor
    ]
    assert _field(chat, "flavor")["options"] == [f.value for f in OpenChatFlavor]
    # The drift this endpoint exists to end.
    assert "vllm" in _field(resp, "flavor")["options"]


@pytest.mark.asyncio
async def test_openrouter_marks_its_key_required(client) -> None:
    body = (await client.get("/v1/llm_providers/_types")).json()
    fields = body[LLMProviderType.OPENROUTER.value]["config_fields"]
    assert _keys(fields) == ["api_key", "app_name", "app_url"]
    assert _field(fields, "api_key")["required"] is True


@pytest.mark.asyncio
async def test_aggregated_declares_its_own_editor(client) -> None:
    """An ordered member list is not a flat field set, so the type says so
    instead of pretending it has none."""
    body = (await client.get("/v1/llm_providers/_types")).json()
    agg = body[LLMProviderType.AGGREGATED.value]
    assert agg["variant"] == "aggregated"
    assert agg["config_fields"] == []


@pytest.mark.asyncio
async def test_llm_rows_declare_no_models_field(client) -> None:
    """models[] left LLMProvider when ModelProfile became the registry of
    what a provider serves (primer/model/providers/llm.py:317-322)."""
    body = (await client.get("/v1/llm_providers/_types")).json()
    for meta in body.values():
        assert meta["row_fields"] == []


@pytest.mark.asyncio
async def test_embedding_types_require_at_least_one_model(client) -> None:
    r = await client.get("/v1/embedding_providers/_types")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {t.value for t in EmbeddingProviderType}
    for meta in body.values():
        models = _field(meta["row_fields"], "models")
        assert models["type"] == "model_list"
        assert models["required"] is True
    hf = body[EmbeddingProviderType.HUGGINGFACE.value]["config_fields"]
    assert _keys(hf) == ["token"]
    assert _field(hf, "token")["required"] is True
    openai = body[EmbeddingProviderType.OPENAI.value]["config_fields"]
    assert _field(openai, "flavor")["options"] == [
        f.value for f in OpenAIEmbeddingFlavor
    ]


@pytest.mark.asyncio
async def test_cross_encoder_token_is_optional(client) -> None:
    """Public reranker repos need no token, so a required box would be a
    lie that blocks the common case."""
    r = await client.get("/v1/cross_encoder_providers/_types")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {t.value for t in CrossEncoderProviderType}
    fields = body[CrossEncoderProviderType.HUGGINGFACE.value]["config_fields"]
    assert _keys(fields) == ["token"]
    assert _field(fields, "token")["required"] is False


@pytest.mark.asyncio
async def test_every_model_family_type_declares_its_limits_block(client) -> None:
    """limits is required on the row and max_concurrency inside it has no
    default (primer/model/providers/_shared.py:44-49), so a form told
    nothing about limits can only ever produce a 422."""
    for plural in ("llm_providers", "embedding_providers", "cross_encoder_providers"):
        body = (await client.get(f"/v1/{plural}/_types")).json()
        assert body, plural
        for type_name, meta in body.items():
            assert meta["limits"] is True, f"{plural}.{type_name}"


@pytest.mark.asyncio
async def test_the_literal_types_path_beats_the_crud_get_by_id(client) -> None:
    """Mounted before the CRUD router, so "_types" is never read as an id."""
    assert (await client.get("/v1/llm_providers/_types")).status_code == 200
    assert (await client.get("/v1/llm_providers/_nope")).status_code == 404
