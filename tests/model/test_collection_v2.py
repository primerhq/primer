"""v2 Collection model: optional search block replaces the embedder trio."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from primer.model.collection import (
    ChunkingConfig,
    Collection,
    CollectionEmbedder,
    CollectionSearchConfig,
)
from primer.model.search import CollectionCrossEncoder


def test_collection_needs_no_embedder_or_ssp() -> None:
    c = Collection(description="grep-only wiki")
    assert c.search is None
    assert c.system is False
    assert c.id.startswith("collection-")


def test_search_block_carries_providers_and_chunking() -> None:
    cfg = CollectionSearchConfig(
        embedder=CollectionEmbedder(provider_id="emb-1", model="text-embedding-3-small"),
        vector_store_provider_id="ssp-1",
        cross_encoder=CollectionCrossEncoder(provider_id="ce-1", model="bge-reranker-v2-m3"),
    )
    assert cfg.chunking == ChunkingConfig(max_chars=1500, overlap=200)
    assert cfg.state == "indexing"
    assert cfg.error is None
    c = Collection(description="d", search=cfg)
    assert c.search.vector_store_provider_id == "ssp-1"


def test_search_state_literal_is_validated() -> None:
    with pytest.raises(ValidationError):
        CollectionSearchConfig(
            embedder=CollectionEmbedder(provider_id="e", model="m"),
            vector_store_provider_id="s",
            state="bogus",
        )


def test_old_trio_fields_are_gone() -> None:
    fields = set(Collection.model_fields)
    assert "embedder" not in fields
    assert "search_provider_id" not in fields
