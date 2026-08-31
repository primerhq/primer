"""HTTP-surface tests for mask-sentinel preservation on provider PUTs.

01a05198: ``make_crud_router``'s PUT is a full replace, and GET serves
every ``SecretStr`` field masked. Before ``on_pre_update=
preserve_masked_secrets_on_update`` was wired onto these routers, a PUT
that round-tripped the served mask (or a UI that blanked an untouched
secret field) would corrupt or erase the real credential - exactly the
gap Dev-Prime's interim "required retype gate" in provider-form.jsx
worked around client-side. These tests exercise a representative sample
of the wired families (LLMProvider's tail-revealing ApiKeySecret,
Toolset's dict[str, SecretStr] env, ArtifactStorageProvider's plain
SecretStr) through the real HTTP router + storage layer - the exhaustive
mask-recognition logic itself is unit-tested in tests/test_common.py.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from primer.model.common import dump_for_storage
from primer.model.provider import (
    AnthropicConfig,
    ArtifactStorageProvider,
    ArtifactStorageProviderType,
    CrossEncoderModel,
    CrossEncoderProvider,
    CrossEncoderProviderType,
    HuggingFaceCrossEncoderConfig,
    Limits,
    LLMProvider,
    LLMProviderType,
    McpConfig,
    S3ArtifactConfig,
    StdioConfig,
    Toolset,
    ToolsetProviderType,
    TransportType,
)


async def _stored(app, model_cls, entity_id: str):
    """Read the row straight from storage, bypassing the API's masking."""
    storage = app.state.storage_provider.get_storage(model_cls)
    stored = await storage.get(entity_id)
    assert stored is not None
    return stored


@pytest.mark.asyncio
async def test_llm_provider_put_round_trip_preserves_masked_api_key(client, app):
    provider = LLMProvider(
        id="anthropic-mask-1",
        provider=LLMProviderType.ANTHROPIC,
        config=AnthropicConfig(api_key=SecretStr("sk-real-key-4242")),
        limits=Limits(max_concurrency=4),
    )
    r = await client.post("/v1/llm_providers", json=dump_for_storage(provider))
    assert r.status_code == 201, r.text

    got = await client.get("/v1/llm_providers/anthropic-mask-1")
    assert got.status_code == 200, got.text
    fetched = got.json()
    assert fetched["config"]["api_key"] == "**********4242"

    # Verbatim GET -> PUT round-trip, only an unrelated field touched.
    put_body = {**fetched, "limits": {**fetched["limits"], "max_concurrency": 8}}
    put = await client.put("/v1/llm_providers/anthropic-mask-1", json=put_body)
    assert put.status_code == 200, put.text
    assert put.json()["limits"]["max_concurrency"] == 8

    stored = await _stored(app, LLMProvider, "anthropic-mask-1")
    assert stored.config.api_key.get_secret_value() == "sk-real-key-4242", (
        "mask round-trip must not corrupt the stored plaintext api_key"
    )


@pytest.mark.asyncio
async def test_llm_provider_put_with_new_api_key_replaces(client, app):
    provider = LLMProvider(
        id="anthropic-mask-2",
        provider=LLMProviderType.ANTHROPIC,
        config=AnthropicConfig(api_key=SecretStr("sk-old-1111")),
        limits=Limits(max_concurrency=4),
    )
    await client.post("/v1/llm_providers", json=dump_for_storage(provider))

    got = await client.get("/v1/llm_providers/anthropic-mask-2")
    fetched = got.json()
    fetched["config"]["api_key"] = "sk-brand-new-9999"
    put = await client.put("/v1/llm_providers/anthropic-mask-2", json=fetched)
    assert put.status_code == 200, put.text
    assert put.json()["config"]["api_key"] == "**********9999"

    stored = await _stored(app, LLMProvider, "anthropic-mask-2")
    assert stored.config.api_key.get_secret_value() == "sk-brand-new-9999"


@pytest.mark.asyncio
async def test_toolset_env_dict_masked_values_preserved(client, app):
    """dict[str, SecretStr] shape (toolset stdio env), matched by key."""
    toolset = Toolset(
        id="ts-mask-1",
        provider=ToolsetProviderType.MCP,
        config=McpConfig(
            transport=TransportType.STDIO,
            config=StdioConfig(
                command=["echo"],
                env={"API_TOKEN": SecretStr("tok-real-value-7777")},
            ),
        ),
    )
    r = await client.post("/v1/toolsets", json=dump_for_storage(toolset))
    assert r.status_code == 201, r.text

    got = await client.get("/v1/toolsets/ts-mask-1")
    fetched = got.json()
    # toolset env/headers keep the plain, tail-less mask (_shared.py's own
    # comment: ApiKeySecret is applied ONLY to LLM/embedding api_key
    # fields, not to toolset env/headers).
    assert fetched["config"]["config"]["env"]["API_TOKEN"] == "**********"

    put_body = {**fetched, "description": "renamed"}
    put = await client.put("/v1/toolsets/ts-mask-1", json=put_body)
    assert put.status_code == 200, put.text

    stored = await _stored(app, Toolset, "ts-mask-1")
    assert (
        stored.config.config.env["API_TOKEN"].get_secret_value()
        == "tok-real-value-7777"
    ), "mask round-trip must not corrupt a dict[str, SecretStr] entry"


@pytest.mark.asyncio
async def test_artifact_storage_provider_masked_secret_pair_preserved(client, app):
    """Plain (tail-less) SecretStr shape: access_key / secret_key."""
    provider = ArtifactStorageProvider(
        id="s3-mask-1",
        description="s3 test",
        provider=ArtifactStorageProviderType.S3,
        config=S3ArtifactConfig(
            bucket="b",
            access_key=SecretStr("AKIA-real"),
            secret_key=SecretStr("s3cret-real"),
        ),
    )
    r = await client.post(
        "/v1/artifact_storage_providers", json=dump_for_storage(provider),
    )
    assert r.status_code == 201, r.text

    got = await client.get("/v1/artifact_storage_providers/s3-mask-1")
    fetched = got.json()
    assert fetched["config"]["access_key"] == "**********"
    assert fetched["config"]["secret_key"] == "**********"

    put_body = {**fetched, "description": "s3 renamed"}
    put = await client.put("/v1/artifact_storage_providers/s3-mask-1", json=put_body)
    assert put.status_code == 200, put.text

    stored = await _stored(app, ArtifactStorageProvider, "s3-mask-1")
    assert stored.config.access_key.get_secret_value() == "AKIA-real"
    assert stored.config.secret_key.get_secret_value() == "s3cret-real"


@pytest.mark.asyncio
async def test_optional_secret_blank_on_put_still_nulls(client, app):
    """01a05198's required "optional-blank nulling" scenario: unlike the
    mask-literal echo, a deliberately blanked OPTIONAL secret field must
    still clear the stored credential, not be treated as "unchanged"."""
    provider = CrossEncoderProvider(
        id="ce-mask-1",
        provider=CrossEncoderProviderType.HUGGINGFACE,
        models=[CrossEncoderModel(name="BAAI/bge-reranker-v2-m3")],
        config=HuggingFaceCrossEncoderConfig(
            token=SecretStr("hf-real-token")
        ),
        limits=Limits(max_concurrency=2),
    )
    r = await client.post(
        "/v1/cross_encoder_providers", json=dump_for_storage(provider),
    )
    assert r.status_code == 201, r.text

    got = await client.get("/v1/cross_encoder_providers/ce-mask-1")
    fetched = got.json()
    assert fetched["config"]["token"] == "**********"

    fetched["config"]["token"] = ""
    put = await client.put("/v1/cross_encoder_providers/ce-mask-1", json=fetched)
    assert put.status_code == 200, put.text

    stored = await _stored(app, CrossEncoderProvider, "ce-mask-1")
    cleared = stored.config.token is None or stored.config.token.get_secret_value() == ""
    assert cleared, "a deliberately blanked optional secret must be cleared, not preserved"
