"""pgvectorscale-backed VectorStore.

Extends :class:`matrix.vector.pgvector.PgVectorStoreProvider` with the
``vectorscale`` Postgres extension installed alongside ``vector``.
pgvectorscale is layered on top of pgvector and adds the
StreamingDiskANN index plus Statistical Binary Quantization (SBQ).

Two index modes are supported, gated by
:attr:`matrix.model.provider.PgVectorScaleConfig.enable_diskann`:

* ``enable_diskann = False`` -- per-collection tables get an HNSW
  index (identical layout to the plain pgvector provider). The
  ``vectorscale`` extension is still installed for opportunistic
  use by future features.
* ``enable_diskann = True`` -- per-collection tables get a
  ``StreamingDiskANN`` index with parameters from the
  ``diskann_*`` config fields. The query-time
  ``diskann.query_search_list_size`` GUC is set on every pooled
  connection. Maintenance (vacuum / analyze / reindex) works
  identically; the bloat heuristic uses a smaller per-row footprint
  estimate to reflect DiskANN's compact graph.
"""

from __future__ import annotations

import logging
from typing import ClassVar

import asyncpg

from primer.model.provider import PgVectorScaleConfig
from primer.vector.pgvector import PgVectorStore, PgVectorStoreProvider


logger = logging.getLogger(__name__)


class PgVectorScaleStoreProvider(PgVectorStoreProvider):
    """VectorStore provider backed by pgvectorscale on Postgres.

    Same per-collection table layout and same :meth:`maintain_indexes`
    workflow as :class:`PgVectorStoreProvider`. The ``vectorscale``
    extension is installed in addition to ``vector`` during
    ``initialize``. When ``enable_diskann`` is set, the per-collection
    ANN index is StreamingDiskANN; otherwise it is HNSW.
    """

    _required_extensions: ClassVar[tuple[str, ...]] = ("vector", "vectorscale")

    def __init__(self, config: PgVectorScaleConfig) -> None:
        super().__init__(config)
        # Re-bind ``self._config`` with the more specific subtype so
        # downstream methods can see the diskann_* fields without an
        # isinstance check.
        self._config: PgVectorScaleConfig = config

    @property
    def config(self) -> PgVectorScaleConfig:  # type: ignore[override]
        return self._config

    async def _apply_search_guc(self, conn: asyncpg.Connection) -> None:
        """Set the right query-time GUC depending on the index in use."""
        if self._config.enable_diskann:
            await conn.execute(
                f"SET diskann.query_search_list_size = "
                f"{int(self._config.diskann_search_list_size)}"
            )
        else:
            await super()._apply_search_guc(conn)

    def _make_store(self) -> PgVectorStore:
        return PgVectorScaleStore(provider=self)

    def _expected_index_size_bytes(
        self,
        *,
        index_kind: str,
        n_rows: int,
    ) -> int:
        """Per-index-kind size estimate for the maintenance heuristic.

        DiskANN with ``memory_optimized`` storage stores SBQ-quantised
        vectors plus a graph; the per-row footprint is dominated by
        ``num_neighbors`` pointers (~ 8 bytes each) and a small
        per-dimension byte count for SBQ. The conservative estimate
        below intentionally over-shoots so we don't reindex too
        eagerly.
        """
        if index_kind == "diskann":
            # ~ 8 bytes per neighbour edge + a small amortised per-row
            # SBQ payload. Doesn't account for dimensions (which we
            # don't easily have here); treats SBQ as a flat ~32 byte
            # adder per row.
            return n_rows * (
                self._config.diskann_num_neighbors * 8 + 32
            )
        return super()._expected_index_size_bytes(
            index_kind=index_kind, n_rows=n_rows
        )


class PgVectorScaleStore(PgVectorStore):
    """pgvectorscale-backed :class:`VectorStore`.

    Reuses every read/write path from :class:`PgVectorStore`.
    Overrides only the ANN index DDL and the catalogue-stored index
    kind / suffix when ``enable_diskann`` is True.
    """

    def _index_kind(self) -> str:
        cfg: PgVectorScaleConfig = self._provider.config  # type: ignore[assignment]
        return "diskann" if cfg.enable_diskann else "hnsw"

    def _render_index_ddl(
        self,
        *,
        table_name: str,
        index_name: str,
        opclass: str,
    ) -> str:
        cfg: PgVectorScaleConfig = self._provider.config  # type: ignore[assignment]
        if not cfg.enable_diskann:
            # Defer to the HNSW DDL the parent renders.
            return super()._render_index_ddl(
                table_name=table_name,
                index_name=index_name,
                opclass=opclass,
            )

        options: list[str] = [
            f"storage_layout = {cfg.diskann_storage_layout}",
            f"num_neighbors = {int(cfg.diskann_num_neighbors)}",
            f"search_list_size = {int(cfg.diskann_search_list_size)}",
            f"max_alpha = {float(cfg.diskann_max_alpha)}",
        ]
        if (
            cfg.diskann_num_bits_per_dimension is not None
            and cfg.diskann_storage_layout == "memory_optimized"
        ):
            options.append(
                f"num_bits_per_dimension = "
                f"{int(cfg.diskann_num_bits_per_dimension)}"
            )

        return (
            f'CREATE INDEX IF NOT EXISTS "{index_name}" '
            f'ON "{self._schema}"."{table_name}" '
            f'USING diskann (vector {opclass}) '
            f'WITH ({", ".join(options)})'
        )
