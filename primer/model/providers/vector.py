"""VectorStore / SemanticSearch provider configuration.

Defines the pgvector-family and LanceDB backend configs plus the two
top-level entities that carry them: the internal
:class:`VectorStoreProviderConfig` adapter shape and the public
runtime-CRUD :class:`SemanticSearchProvider` entity. The Postgres base
config + pool settings are shared with the storage family and imported
from there.
"""

from __future__ import annotations

from pathlib import Path
from enum import Enum
from typing import ClassVar, Literal

from pydantic import BaseModel, Field, PositiveInt, model_validator

from primer.model.common import Identifiable
from primer.model.providers.storage import _PostgresBaseConfig


# Internal adapter shape; not exposed via API.
# See SemanticSearchProvider for the public-facing entity.
class VectorStoreProviderType(str, Enum):
    """Supported VectorStore provider backends."""

    PGVECTOR = "pgvector"
    PGVECTORSCALE = "pgvectorscale"
    LANCE = "lance"


_DistanceMetric = Literal["cosine", "l2", "ip"]


class _PgVectorBaseConfig(_PostgresBaseConfig):
    """Common HNSW + distance options shared by pgvector-family providers."""

    distance_metric: _DistanceMetric = Field(
        default="cosine",
        description=(
            "Distance metric for the vector index. 'cosine' for normalised "
            "embeddings (most common), 'l2' for Euclidean, 'ip' for inner "
            "product."
        ),
    )
    hnsw_m: PositiveInt = Field(
        default=16,
        description=(
            "HNSW 'm' parameter -- max connections per node. Higher = better "
            "recall, larger index, slower build. pgvector default is 16."
        ),
    )
    hnsw_ef_construction: PositiveInt = Field(
        default=64,
        description=(
            "HNSW 'ef_construction' -- candidate list size during build. "
            "Higher = better recall, slower build. pgvector default is 64."
        ),
    )
    hnsw_ef_search: PositiveInt = Field(
        default=40,
        description=(
            "Query-time 'hnsw.ef_search' GUC -- candidate list size during "
            "queries. Higher = better recall, slower queries. pgvector "
            "default is 40."
        ),
    )
    reindex_cron: str | None = Field(
        default=None,
        description=(
            "Crontab expression scheduling periodic HNSW maintenance via "
            ":meth:`primer.int.VectorStoreProvider.maintain_indexes`. "
            "None disables scheduling (caller drives maintenance manually)."
        ),
    )
    use_halfvec: bool = Field(
        default=False,
        description=(
            "Store vectors as pgvector half-precision (halfvec, up to 4000 "
            "dimensions) instead of the standard vector type (up to 2000). "
            "Enable for embedding models above 2000 dimensions, e.g. "
            "text-embedding-3-large (3072). Only affects collections created "
            "while enabled; existing collections keep their original type."
        ),
    )

    @model_validator(mode="after")
    def _validate_cron(self) -> "_PgVectorBaseConfig":
        if self.reindex_cron is not None:
            try:
                from croniter import croniter
            except ImportError as exc:  # pragma: no cover - dep is in pyproject
                raise ValueError(
                    "reindex_cron is set but croniter is not installed"
                ) from exc
            if not croniter.is_valid(self.reindex_cron):
                raise ValueError(
                    f"reindex_cron {self.reindex_cron!r} is not a valid crontab expression"
                )
        return self


class PgVectorConfig(_PgVectorBaseConfig):
    """Connection settings for the pgvector VectorStore provider.

    Requires the ``vector`` extension to be installable on the target
    database (the provider runs ``CREATE EXTENSION IF NOT EXISTS vector``
    on initialise).
    """


class PgVectorScaleConfig(_PgVectorBaseConfig):
    """Connection settings for the pgvectorscale VectorStore provider.

    Requires the ``vector`` AND ``vectorscale`` extensions. pgvectorscale
    layers on top of pgvector and adds the StreamingDiskANN index, SBQ
    quantization, and tuned HNSW behaviour. When ``enable_diskann`` is
    True the per-collection vector tables get a DiskANN index instead
    of HNSW; the ``diskann_*`` fields below tune that index. When
    ``enable_diskann`` is False the provider behaves exactly like
    :class:`PgVectorConfig` plus the ``vectorscale`` extension being
    installed for opportunistic use.
    """

    enable_diskann: bool = Field(
        default=False,
        description=(
            "When True, create StreamingDiskANN indexes (from "
            "pgvectorscale) instead of pgvector's HNSW. DiskANN is "
            "the right choice for very large collections (10M+ "
            "vectors) where HNSW's memory cost becomes prohibitive."
        ),
    )
    diskann_storage_layout: Literal["memory_optimized", "plain"] = Field(
        default="memory_optimized",
        description=(
            "DiskANN storage layout. ``memory_optimized`` enables "
            "Statistical Binary Quantization (SBQ) -- the default and "
            "the recommended choice; ``plain`` keeps full-precision "
            "vectors in the index."
        ),
    )
    diskann_num_neighbors: PositiveInt = Field(
        default=50,
        description=(
            "DiskANN graph degree -- number of neighbours stored per "
            "node. Higher = better recall, larger index. Default 50."
        ),
    )
    diskann_search_list_size: PositiveInt = Field(
        default=100,
        description=(
            "DiskANN ``search_list_size`` build parameter AND the "
            "default query-time ``diskann.query_search_list_size`` "
            "GUC. Higher = better recall, slower queries / build."
        ),
    )
    diskann_max_alpha: float = Field(
        default=1.2,
        gt=1.0,
        description=(
            "DiskANN graph density. Higher (up to ~1.4) increases "
            "recall at the cost of build time. Default 1.2."
        ),
    )
    diskann_num_bits_per_dimension: PositiveInt | None = Field(
        default=None,
        description=(
            "Bits per dimension used by SBQ when storage_layout is "
            "``memory_optimized``. None lets pgvectorscale pick the "
            "default (typically 2). Ignored when storage_layout is "
            "``plain``."
        ),
    )


class LanceConfig(BaseModel):
    """LanceDB embedded-mode SemanticSearchProvider configuration.

    Persists every collection's vector table as a Lance dataset under
    ``path``. The directory is created with mode 0o700 on first use.
    Multiple LanceDB SSPs can coexist as long as they use different
    paths. Single-process write-safe; multi-process primer-api +
    primer-worker against the same path is out of scope (spec §9).
    """

    path: Path = Field(
        ...,
        description=(
            "Filesystem directory holding the LanceDB datasets. Created "
            "on initialise if missing. Must be writable by the primer "
            "process. Use an absolute path."
        ),
    )
    hnsw_m: PositiveInt = Field(
        default=16,
        description=(
            "HNSW graph degree. Mirrors PgVectorConfig.hnsw_m so the "
            "create modal can share one knobs section across backends."
        ),
    )
    hnsw_ef_construction: PositiveInt = Field(
        default=64,
        description=(
            "HNSW 'ef_construction' -- candidate list size during "
            "index build. Higher = better recall, slower build. "
            "Mirrors PgVectorConfig.hnsw_ef_construction's default."
        ),
    )
    hnsw_ef_search: PositiveInt = Field(
        default=40,
        description=(
            "HNSW query-time candidate list size. Higher = better "
            "recall, slower queries. Mirrors PgVectorConfig.hnsw_ef_search "
            "(the pgvector variant exposes this via the hnsw.ef_search GUC)."
        ),
    )
    index_min_rows: PositiveInt = Field(
        default=1000,
        description=(
            "Skip ANN-index construction until a collection has at least "
            "this many rows. Below the threshold, search runs brute-force."
        ),
    )


# Internal adapter shape; not exposed via API.
# See SemanticSearchProvider for the public-facing entity.
class VectorStoreProviderConfig(BaseModel):
    """Top-level VectorStore provider configuration -- discriminated by ``provider``."""

    provider: VectorStoreProviderType = Field(
        ...,
        description="Which VectorStore backend to use.",
    )
    config: PgVectorConfig | PgVectorScaleConfig | LanceConfig = Field(
        ...,
        description="Backend-specific connection settings; must match ``provider``.",
    )

    @model_validator(mode="after")
    def _validate_config_matches(self) -> "VectorStoreProviderConfig":
        expected = {
            VectorStoreProviderType.PGVECTOR: PgVectorConfig,
            VectorStoreProviderType.PGVECTORSCALE: PgVectorScaleConfig,
            VectorStoreProviderType.LANCE: LanceConfig,
        }[self.provider]
        if not isinstance(self.config, expected):
            raise ValueError(
                f"provider={self.provider.value!r} requires a "
                f"{expected.__name__} in 'config'"
            )
        return self


# ===========================================================================
# SemanticSearch provider entity (runtime-CRUD, replaces VectorStoreProviderConfig)
# ===========================================================================


class SemanticSearchProviderType(str, Enum):
    """Supported semantic-search backends.

    Mirrors VectorStoreProviderType (which will be removed once all
    callsites migrate to SemanticSearchProvider).
    """

    PGVECTOR = "pgvector"
    PGVECTORSCALE = "pgvectorscale"
    LANCE = "lance"


class SemanticSearchProvider(Identifiable):
    """Operator-managed semantic-search backend backing collections
    and the internal collections subsystem.

    Stored as a CRUD-able row alongside LLMProvider, EmbeddingProvider,
    etc. The discriminated ``config`` carries backend-specific
    connection + index settings; the parent ``provider`` discriminator
    chooses which config shape is valid.
    """

    _id_prefix: ClassVar[str] = "semantic-search-provider"

    provider: SemanticSearchProviderType = Field(
        ...,
        description="Which semantic-search backend to use.",
    )
    config: PgVectorConfig | PgVectorScaleConfig | LanceConfig = Field(
        ...,
        description="Backend-specific connection settings; must match ``provider``.",
    )

    @model_validator(mode="after")
    def _validate_config_matches(self) -> "SemanticSearchProvider":
        expected = {
            SemanticSearchProviderType.PGVECTOR: PgVectorConfig,
            SemanticSearchProviderType.PGVECTORSCALE: PgVectorScaleConfig,
            SemanticSearchProviderType.LANCE: LanceConfig,
        }[self.provider]
        if not isinstance(self.config, expected):
            raise ValueError(
                f"provider={self.provider.value!r} requires a "
                f"{expected.__name__} in 'config'"
            )
        return self
