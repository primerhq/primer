"""Abstract base class for VectorStore providers.

A *VectorStore provider* owns the shared vector-database state
(connection pool, extension setup, per-collection table cache) and
exposes the single :class:`matrix.int.VectorStore` handle that talks
to it.

In contrast to :class:`matrix.int.StorageProvider` (which yields one
:class:`Storage` per model class), a VectorStore provider yields one
:class:`VectorStore` -- collections live as separate tables in the
same database, and the single VectorStore dispatches to the right
table by ``collection_id`` internally.

The provider also exposes :meth:`maintain_indexes`, which scans every
managed vector table and applies the appropriate vacuum / analyze /
reindex action to keep HNSW (and similar approximate-NN) indexes
performant. HNSW indexes degrade as the underlying table accumulates
inserts, updates, and deletes; the analyzer surfaces tables that have
crossed maintenance thresholds and the same call applies the
recommended action concurrently (non-blocking).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from primer.int.vector_store import VectorStore


# ---------- Maintenance report ---------------------------------------------


MaintenanceAction = Literal[
    "none",
    "analyze",
    "vacuum_analyze",
    "reindex",
]
"""Actions :meth:`VectorStoreProvider.maintain_indexes` may take per table.

* ``"none"`` -- table is healthy, no work performed.
* ``"analyze"`` -- table needed a planner statistics refresh; cheap,
  non-blocking.
* ``"vacuum_analyze"`` -- table had significant dead-tuple bloat; reclaims
  space and refreshes statistics.
* ``"reindex"`` -- index bloat or large modification volume crossed the
  threshold; backend ran ``REINDEX INDEX CONCURRENTLY`` (non-blocking
  to readers and writers).
"""


class MaintenanceReport(BaseModel):
    """Per-table outcome from one :meth:`VectorStoreProvider.maintain_indexes` run."""

    collection_id: str = Field(
        ...,
        description="Collection whose vector table was inspected.",
    )
    table_name: str = Field(
        ...,
        description="Backend-side table name (sanitised form of the collection id).",
    )
    action: MaintenanceAction = Field(
        ...,
        description="Action that was taken (``'none'`` if no maintenance was needed).",
    )
    n_live_tup: int | None = Field(
        default=None,
        description="Live tuple count observed at analysis time (may be approximate).",
    )
    n_dead_tup: int | None = Field(
        default=None,
        description="Dead tuple count observed at analysis time.",
    )
    n_mod_since_analyze: int | None = Field(
        default=None,
        description="Tuples modified since the last ANALYZE.",
    )
    duration_seconds: float = Field(
        ...,
        ge=0,
        description="Wall-clock time the maintenance action took (0 for ``'none'``).",
    )
    started_at: datetime = Field(
        ...,
        description="UTC instant the analysis began.",
    )
    detail: str | None = Field(
        default=None,
        description=(
            "Free-form note. Populated when an action was skipped or had "
            "an unusual outcome (e.g. 'no HNSW index found')."
        ),
    )


# ---------- Provider ABC --------------------------------------------------


class VectorStoreProvider(ABC):
    """Backend-agnostic factory + lifecycle owner for a :class:`VectorStore`.

    Subclasses bind to one backend (pgvector, pgvectorscale, Qdrant,
    Pinecone, etc.) and one provider-specific config. ``initialize``
    opens the pool, installs required extensions (``vector`` and
    optionally ``vectorscale`` for the Postgres-family providers), and
    runs any one-time schema setup. ``aclose`` tears down the pool.
    """

    @abstractmethod
    async def initialize(self) -> None:
        """Open the connection pool, install required extensions, prepare schema.

        Idempotent: calling on an already-initialised provider is a
        no-op. MUST be awaited before the first :meth:`get_vector_store`
        handle is used.
        """

    @abstractmethod
    async def aclose(self) -> None:
        """Close the connection pool and release backend resources.

        Idempotent: calling on a never-initialised or already-closed
        provider is a no-op.
        """

    @abstractmethod
    def get_vector_store(self) -> VectorStore:
        """Return the :class:`VectorStore` handle backed by this provider.

        Cached: every call returns the same instance.
        """

    @abstractmethod
    async def maintain_indexes(self) -> list[MaintenanceReport]:
        """Scan every managed vector table and apply maintenance where needed.

        Heuristic per table:

        * If ``n_dead_tup / (n_live_tup + n_dead_tup) > 0.20`` and the
          table has any rows -> ``VACUUM ANALYZE``.
        * Otherwise, if ``n_mod_since_analyze > 0.10 * max(n_live_tup, 1)``
          -> ``ANALYZE``.
        * Otherwise, if the HNSW index size has grown out of proportion
          to the underlying table (heuristic: index/table size ratio
          drift past a threshold) -> ``REINDEX INDEX CONCURRENTLY``.
        * Otherwise -> no action.

        Returns one :class:`MaintenanceReport` per inspected table,
        regardless of whether action was taken (``action="none"`` for
        healthy tables). Failures on individual tables are logged and
        captured in the corresponding report's ``detail`` field; one
        bad table does not abort the run.

        This method is intended to be invoked manually for ad-hoc
        maintenance, AND to be scheduled periodically by an external
        worker driven by the provider config's ``reindex_cron`` field.
        """
