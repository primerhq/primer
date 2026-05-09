"""Unit tests for VectorStoreConfig."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from matrix.model.vector import VectorStoreConfig


class TestVectorStoreConfig:
    def test_minimal_construction(self) -> None:
        cfg = VectorStoreConfig(
            id="_active_vector_store",
            backend="pgvector",
            settings={"dsn": "postgres://x/y"},
        )
        assert cfg.id == "_active_vector_store"
        assert cfg.backend == "pgvector"
        assert cfg.settings == {"dsn": "postgres://x/y"}

    def test_unknown_backend_rejected(self) -> None:
        with pytest.raises(ValidationError):
            VectorStoreConfig(
                id="_active_vector_store",
                backend="not-a-backend",  # type: ignore[arg-type]
                settings={},
            )

    def test_round_trip(self) -> None:
        cfg = VectorStoreConfig(
            id="_active_vector_store",
            backend="pgvectorscale",
            settings={"dsn": "postgres://x/y", "schema": "vec"},
        )
        rehydrated = VectorStoreConfig.model_validate(cfg.model_dump())
        assert rehydrated == cfg
