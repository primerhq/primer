"""Unit tests for Collection / CollectionSearchConfig and the surviving
retrieval knob, CollectionCrossEncoder.

S2 removed CollectionSearch and MmrConfig outright. The classes that
covered them went with them; what is left is the Collection fields that
still exist plus the cross-encoder config.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from primer.model.collection import (
    Collection,
    CollectionEmbedder,
    CollectionSearchConfig,
)
from primer.model.search import CollectionCrossEncoder


# ===========================================================================
# Collection.search default
# ===========================================================================


class TestCollectionSearchField:
    def test_search_defaults_to_none(self) -> None:
        c = Collection(id="c1", description="t")
        assert c.search is None

    def test_round_trip_with_search_none(self) -> None:
        original = Collection(id="c1", description="t")
        data = original.model_dump()
        assert data["search"] is None
        assert Collection.model_validate(data).search is None

    def test_round_trip_with_a_search_block(self) -> None:
        original = Collection(
            id="c1",
            description="t",
            search=CollectionSearchConfig(
                embedder=CollectionEmbedder(provider_id="p", model="m"),
                vector_store_provider_id="ssp-test",
            ),
        )
        rehydrated = Collection.model_validate(original.model_dump())
        assert rehydrated.search is not None
        assert rehydrated.search.vector_store_provider_id == "ssp-test"
        assert rehydrated.search.state == "indexing"

    def test_legacy_collection_json_without_search_field_loads(self) -> None:
        """Backwards compatibility: JSON predating the field deserialises
        cleanly because the field defaults to None."""
        legacy = {"id": "c1", "description": "t"}
        c = Collection.model_validate(legacy)
        assert c.search is None
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
# Collection.system flag
# ===========================================================================


class TestCollectionSystemFlag:
    def test_system_defaults_to_false(self) -> None:
        c = Collection(id="c1", description="t")
        assert c.system is False

    def test_system_true_round_trips(self) -> None:
        original = Collection(
            id="_catalog_agents",
            description="System collection",
            system=True,
        )
        rehydrated = Collection.model_validate(original.model_dump())
        assert rehydrated.system is True

    def test_legacy_json_without_system_field_loads_as_user(self) -> None:
        # Backwards compatibility: JSON predating the field deserialises
        # cleanly with system=False (treated as a normal user collection).
        legacy = {"id": "c1", "description": "t"}  # no system/search keys
        c = Collection.model_validate(legacy)
        assert c.system is False
        assert c.search is None
