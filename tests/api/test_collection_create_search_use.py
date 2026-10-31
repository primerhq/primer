"""A collection create whose ``search`` block names providers the saver may not use is refused (security review of P4, finding 2).

``PUT /v1/collections/{id}/search`` checks provider use; a create carrying the block must run the same check and answer the same
not-registered 409, storing nothing.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from tests.api.conftest import raw_client as client  # noqa: F401
from tests.api.test_global_settings_read import _login, _seed_user

from primer.model.access import Access
from primer.model.collection import Collection
from primer.model.provider import (
    EmbeddingModel, EmbeddingProvider, EmbeddingProviderType, HuggingFaceConfig, Limits, SemanticSearchProvider,
)


async def _providers(sp, suffix: str, access: Access) -> None:
    await sp.get_storage(EmbeddingProvider).create(EmbeddingProvider(
        id=f"emb-{suffix}", provider=EmbeddingProviderType.HUGGINGFACE, models=[EmbeddingModel(name="m")],
        config=HuggingFaceConfig(token=SecretStr("hf_x")), limits=Limits(max_concurrency=1), access=access))
    await sp.get_storage(SemanticSearchProvider).create(SemanticSearchProvider.model_validate({
        "id": f"ssp-{suffix}", "provider": "pgvector", "access": access.model_dump(mode="json"),
        "config": {"hostname": "h", "port": 5432, "database": "d", "username": "u", "password": "p", "db_schema": "public"}}))


def _body(cid: str, suffix: str) -> dict:
    return {"id": cid, "description": "d", "search": {
        "embedder": {"provider_id": f"emb-{suffix}", "model": "m"}, "vector_store_provider_id": f"ssp-{suffix}", "state": "ready"}}


@pytest.mark.asyncio
async def test_create_naming_private_search_providers_is_refused(app, client):
    await _seed_user(app, uid="eve", username="eve", role="user")
    sp = app.state.storage_provider
    await _providers(sp, "priv", Access())
    await _login(client, "eve")

    r = await client.post("/v1/collections", json=_body("eve-kb", "priv"))

    assert r.status_code == 409, r.text
    assert "is not registered" in r.text
    assert await sp.get_storage(Collection).get("eve-kb") is None


@pytest.mark.asyncio
async def test_create_naming_usable_search_providers_is_stored(app, client):
    await _seed_user(app, uid="eve", username="eve", role="user")
    sp = app.state.storage_provider
    await _providers(sp, "pub", Access(public=True))
    await _login(client, "eve")

    r = await client.post("/v1/collections", json=_body("eve-kb2", "pub"))

    assert r.status_code == 201, r.text
    assert (await sp.get_storage(Collection).get("eve-kb2")).search.embedder.provider_id == "emb-pub"
