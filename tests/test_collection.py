"""Unit tests for the Collection / CollectionSearch / MmrConfig / CollectionCrossEncoder models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from matrix.model.collection import Collection, CollectionEmbedder
from matrix.model.search import (
    CollectionCrossEncoder,
    CollectionSearch,
    MmrConfig,
)


# ===========================================================================
# Collection.search default
# ===========================================================================


class TestCollectionSearchField:
    def test_search_defaults_to_none(self) -> None:
        c = Collection(
            id="c1",
            description="t",
            embedder=CollectionEmbedder(provider_id="p", model="m"),
        )
        assert c.search is None

    def test_round_trip_with_search_none(self) -> None:
        original = Collection(
            id="c1",
            description="t",
            embedder=CollectionEmbedder(provider_id="p", model="m"),
        )
        data = original.model_dump()
        assert data["search"] is None
        rehydrated = Collection.model_validate(data)
        assert rehydrated.search is None

    def test_round_trip_with_mmr(self) -> None:
        original = Collection(
            id="c1",
            description="t",
            embedder=CollectionEmbedder(provider_id="p", model="m"),
            search=CollectionSearch(mmr=MmrConfig(lambda_mult=0.7, fetch_k=40)),
        )
        rehydrated = Collection.model_validate(original.model_dump())
        assert rehydrated.search is not None
        assert rehydrated.search.mmr is not None
        assert rehydrated.search.mmr.lambda_mult == 0.7
        assert rehydrated.search.mmr.fetch_k == 40
        assert rehydrated.search.cer is None

    def test_round_trip_with_cer(self) -> None:
        original = Collection(
            id="c1",
            description="t",
            embedder=CollectionEmbedder(provider_id="p", model="m"),
            search=CollectionSearch(
                cer=CollectionCrossEncoder(
                    provider_id="ce",
                    model="BAAI/bge-reranker-v2-m3",
                    top_n=50,
                    batch_size=16,
                ),
            ),
        )
        rehydrated = Collection.model_validate(original.model_dump())
        assert rehydrated.search is not None
        assert rehydrated.search.cer is not None
        assert rehydrated.search.cer.provider_id == "ce"
        assert rehydrated.search.cer.top_n == 50
        assert rehydrated.search.cer.batch_size == 16
        assert rehydrated.search.mmr is None

    def test_round_trip_with_both(self) -> None:
        original = Collection(
            id="c1",
            description="t",
            embedder=CollectionEmbedder(provider_id="p", model="m"),
            search=CollectionSearch(
                mmr=MmrConfig(),
                cer=CollectionCrossEncoder(provider_id="ce", model="m"),
            ),
        )
        rehydrated = Collection.model_validate(original.model_dump())
        assert rehydrated.search is not None
        assert rehydrated.search.mmr is not None
        assert rehydrated.search.cer is not None

    def test_legacy_collection_json_without_search_field_loads(self) -> None:
        """Backwards compatibility: existing JSON that predates the field
        deserialises cleanly because the field defaults to None."""
        legacy = {
            "id": "c1",
            "description": "t",
            "embedder": {"provider_id": "p", "model": "m"},
            # NB: no "search" key.
        }
        c = Collection.model_validate(legacy)
        assert c.search is None


# ===========================================================================
# MmrConfig defaults + validation
# ===========================================================================


class TestMmrConfig:
    def test_defaults(self) -> None:
        cfg = MmrConfig()
        assert cfg.lambda_mult == 0.5
        assert cfg.fetch_k is None

    def test_lambda_mult_lower_bound(self) -> None:
        with pytest.raises(ValidationError):
            MmrConfig(lambda_mult=-0.1)

    def test_lambda_mult_upper_bound(self) -> None:
        with pytest.raises(ValidationError):
            MmrConfig(lambda_mult=1.1)

    def test_lambda_mult_zero_and_one_allowed(self) -> None:
        # Boundaries are inclusive (ge=0, le=1).
        assert MmrConfig(lambda_mult=0.0).lambda_mult == 0.0
        assert MmrConfig(lambda_mult=1.0).lambda_mult == 1.0

    def test_fetch_k_must_be_positive_when_set(self) -> None:
        with pytest.raises(ValidationError):
            MmrConfig(fetch_k=0)
        with pytest.raises(ValidationError):
            MmrConfig(fetch_k=-3)


# ===========================================================================
# CollectionCrossEncoder defaults + validation
# ===========================================================================


class TestCollectionCrossEncoder:
    def test_defaults(self) -> None:
        cfg = CollectionCrossEncoder(provider_id="p", model="m")
        assert cfg.top_n == 100
        assert cfg.batch_size == 32

    def test_provider_id_required_non_empty(self) -> None:
        with pytest.raises(ValidationError):
            CollectionCrossEncoder(provider_id="", model="m")

    def test_model_required_non_empty(self) -> None:
        with pytest.raises(ValidationError):
            CollectionCrossEncoder(provider_id="p", model="")

    def test_top_n_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            CollectionCrossEncoder(provider_id="p", model="m", top_n=0)

    def test_batch_size_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            CollectionCrossEncoder(provider_id="p", model="m", batch_size=0)


# ===========================================================================
# CollectionSearch defaults
# ===========================================================================


class TestCollectionSearch:
    def test_both_optional(self) -> None:
        s = CollectionSearch()
        assert s.mmr is None
        assert s.cer is None


# ===========================================================================
# Collection.system flag
# ===========================================================================


class TestCollectionSystemFlag:
    def test_system_defaults_to_false(self) -> None:
        c = Collection(
            id="c1",
            description="t",
            embedder=CollectionEmbedder(provider_id="p", model="m"),
        )
        assert c.system is False

    def test_system_true_round_trips(self) -> None:
        original = Collection(
            id="_catalog_agents",
            description="System collection",
            embedder=CollectionEmbedder(provider_id="p", model="m"),
            system=True,
        )
        rehydrated = Collection.model_validate(original.model_dump())
        assert rehydrated.system is True

    def test_legacy_json_without_system_field_loads_as_user(self) -> None:
        # Backwards compatibility: JSON predating the field deserialises
        # cleanly with system=False (treated as a normal user collection).
        legacy = {
            "id": "c1",
            "description": "t",
            "embedder": {"provider_id": "p", "model": "m"},
            # NB: no "system" key, no "search" key.
        }
        c = Collection.model_validate(legacy)
        assert c.system is False
        assert c.search is None
