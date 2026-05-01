"""pgvector-backed :class:`VectorStoreProvider` and :class:`VectorStore`.

One vector table per collection (``<schema>.embeddings_<sanitised>``)
with an HNSW index on the vector column (default) and a GIN index on
the ``meta`` JSONB column for fast metadata-containment queries. The
collection catalogue (``<schema>.matrix_collections``) records the
table name, index name, index kind, dimensions, and distance metric
for each collection so the provider can re-create handles after
restart and so :meth:`maintain_indexes` can enumerate every managed
table without scanning ``pg_class``.

Subclasses (e.g. :class:`matrix.vector.pgvectorscale.PgVectorScaleStore`)
override :meth:`PgVectorStore._index_kind`,
:meth:`PgVectorStore._render_index_ddl`, and
:meth:`PgVectorStoreProvider._apply_search_guc` to swap in a different
ANN index type (DiskANN). All other code paths -- catalogue, table
DDL, put/search/get/delete, maintenance heuristic -- are shared.

HNSW maintenance (:meth:`PgVectorStoreProvider.maintain_indexes`)
inspects every catalogued table and applies ``ANALYZE``,
``VACUUM ANALYZE``, or ``REINDEX INDEX CONCURRENTLY`` based on the
heuristic documented on
:meth:`matrix.int.VectorStoreProvider.maintain_indexes`. All forms
run non-blocking to readers and writers.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, ClassVar, Literal

import asyncpg
from pgvector.asyncpg import register_vector

from matrix.int.vector_store import VectorStore
from matrix.int.vector_store_provider import (
    MaintenanceAction,
    MaintenanceReport,
    VectorStoreProvider,
)
from matrix.model.except_ import (
    BadRequestError,
    ConfigError,
    ConflictError,
    ProviderError,
    ServerError,
)
from matrix.model.provider import PgVectorConfig
from matrix.model.vector import EmbeddingRecord, SearchResult, Vector


logger = logging.getLogger(__name__)


# ---------- Identifier helpers --------------------------------------------


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_COLLECTION_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _sanitize_schema(name: str) -> str:
    if not _IDENT_RE.match(name):
        raise ConfigError(
            f"schema {name!r} must match {_IDENT_RE.pattern}"
        )
    return name


def _table_name_for_collection(collection_id: str) -> str:
    """Map a collection id to a vector table name.

    Allows ``[A-Za-z0-9_-]``; hyphens are translated to underscores
    so the result is a bare SQL identifier without quoting concerns.
    """
    if not _COLLECTION_ID_RE.match(collection_id):
        raise BadRequestError(
            f"collection_id {collection_id!r} must match "
            f"{_COLLECTION_ID_RE.pattern} (alphanumeric / underscore / hyphen)"
        )
    return "embeddings_" + collection_id.replace("-", "_")


_DISTANCE_OPCLASS: dict[str, str] = {
    "cosine": "vector_cosine_ops",
    "l2": "vector_l2_ops",
    "ip": "vector_ip_ops",
}

# pgvector distance operators -- used in ``ORDER BY <vec> <op> <query>``.
_DISTANCE_OPERATOR: dict[str, str] = {
    "cosine": "<=>",
    "l2": "<->",
    "ip": "<#>",
}


# ===========================================================================
# Provider
# ===========================================================================


class PgVectorStoreProvider(VectorStoreProvider):
    """VectorStore provider backed by pgvector on Postgres.

    Owns the asyncpg pool, ensures the ``vector`` extension is
    installed, materialises the catalogue table, and yields the
    single :class:`PgVectorStore` handle.
    """

    # Subclasses (e.g. :class:`PgVectorScaleStoreProvider`) override this
    # to install additional extensions in ``initialize``.
    _required_extensions: ClassVar[tuple[str, ...]] = ("vector",)

    def __init__(self, config: PgVectorConfig) -> None:
        self._config = config
        self._schema = _sanitize_schema(config.db_schema)
        self._pool: asyncpg.Pool | None = None
        self._store: PgVectorStore | None = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise ConfigError(
                f"{type(self).__name__} used before initialize()"
            )
        return self._pool

    @property
    def schema(self) -> str:
        return self._schema

    @property
    def config(self) -> PgVectorConfig:
        return self._config

    async def initialize(self) -> None:
        if self._pool is not None:
            return
        cfg = self._config
        try:
            self._pool = await asyncpg.create_pool(
                host=cfg.hostname,
                port=cfg.port,
                user=cfg.username,
                password=cfg.password.get_secret_value(),
                database=cfg.database,
                min_size=cfg.pool.min_size,
                max_size=cfg.pool.max_size,
                timeout=cfg.pool.acquire_timeout,
                max_inactive_connection_lifetime=cfg.pool.max_idle,
                command_timeout=cfg.pool.acquire_timeout,
                init=self._init_connection,
            )
        except Exception as exc:
            raise ProviderError(
                f"failed to open Postgres pool: {exc}",
                cause=exc,
            ) from exc

        async with self._pool.acquire() as conn:
            await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{self._schema}"')
            for ext in self._required_extensions:
                await conn.execute(f'CREATE EXTENSION IF NOT EXISTS "{ext}"')
            # Re-register the vector codec on the pooled connection
            # (the extension may not have existed when init ran).
            await register_vector(conn)
            await conn.execute(
                f'CREATE TABLE IF NOT EXISTS "{self._schema}".matrix_collections ('
                'collection_id text PRIMARY KEY, '
                'table_name text NOT NULL, '
                'index_name text NOT NULL, '
                'index_kind text NOT NULL, '
                'dimensions integer NOT NULL, '
                'distance text NOT NULL, '
                'created_at timestamptz NOT NULL DEFAULT now()'
                ')'
            )

        logger.info(
            "%s initialised (schema=%r, host=%s:%d, exts=%s)",
            type(self).__name__,
            self._schema,
            cfg.hostname,
            cfg.port,
            list(self._required_extensions),
        )

    async def _init_connection(self, conn: asyncpg.Connection) -> None:
        """asyncpg pool ``init`` hook: register the vector codec.

        Called for every freshly opened connection. The first call may
        race the ``CREATE EXTENSION`` -- if so it is a no-op and
        ``initialize`` re-registers afterwards.
        """
        try:
            await register_vector(conn)
        except Exception:
            # Extension may not exist yet on first connection. The
            # ``initialize`` body re-registers explicitly.
            return
        await self._apply_search_guc(conn)

    async def _apply_search_guc(self, conn: asyncpg.Connection) -> None:
        """Set the query-time index search-list size GUC for this session.

        Default implementation sets ``hnsw.ef_search``. Subclasses
        that use a different index type (DiskANN) override to set
        their own GUC.
        """
        await conn.execute(
            f"SET hnsw.ef_search = {int(self._config.hnsw_ef_search)}"
        )

    async def aclose(self) -> None:
        if self._pool is None:
            return
        await self._pool.close()
        self._pool = None
        self._store = None
        logger.info("%s closed", type(self).__name__)

    def get_vector_store(self) -> VectorStore:
        if self._store is None:
            self._store = self._make_store()
        return self._store

    def _make_store(self) -> PgVectorStore:
        """Hook for subclasses (pgvectorscale) to swap the store class."""
        return PgVectorStore(provider=self)

    # ---------- Maintenance --------------------------------------------------

    async def maintain_indexes(self) -> list[MaintenanceReport]:
        if self._pool is None:
            raise ConfigError(
                f"{type(self).__name__}.maintain_indexes called before initialize()"
            )

        catalog_sql = (
            f'SELECT collection_id, table_name, index_name, index_kind '
            f'FROM "{self._schema}".matrix_collections '
            'ORDER BY collection_id'
        )
        async with self._pool.acquire() as conn:
            catalog = await conn.fetch(catalog_sql)

        reports: list[MaintenanceReport] = []
        for row in catalog:
            report = await self._maintain_one(
                collection_id=row["collection_id"],
                table_name=row["table_name"],
                index_name=row["index_name"],
                index_kind=row["index_kind"],
            )
            reports.append(report)
        return reports

    async def _maintain_one(
        self,
        *,
        collection_id: str,
        table_name: str,
        index_name: str,
        index_kind: str,
    ) -> MaintenanceReport:
        started = datetime.now(timezone.utc)
        stats_sql = (
            'SELECT '
            'n_live_tup, n_dead_tup, n_mod_since_analyze '
            'FROM pg_stat_user_tables '
            'WHERE schemaname = $1 AND relname = $2'
        )
        index_size_sql = (
            "SELECT pg_relation_size(c.oid) AS index_size "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = $1 AND c.relname = $2"
        )

        try:
            async with self._pool.acquire() as conn:
                stats_row = await conn.fetchrow(
                    stats_sql, self._schema, table_name
                )
                size_row = await conn.fetchrow(
                    index_size_sql, self._schema, index_name
                )
        except Exception as exc:
            return MaintenanceReport(
                collection_id=collection_id,
                table_name=table_name,
                action="none",
                duration_seconds=0.0,
                started_at=started,
                detail=f"failed to read stats: {exc}",
            )

        if stats_row is None:
            return MaintenanceReport(
                collection_id=collection_id,
                table_name=table_name,
                action="none",
                duration_seconds=0.0,
                started_at=started,
                detail="table absent from pg_stat_user_tables (never written?)",
            )

        n_live = int(stats_row["n_live_tup"] or 0)
        n_dead = int(stats_row["n_dead_tup"] or 0)
        n_mod = int(stats_row["n_mod_since_analyze"] or 0)

        action: MaintenanceAction = "none"
        total = n_live + n_dead
        if total > 0 and n_dead / total > 0.20:
            action = "vacuum_analyze"
        elif n_mod > 0.10 * max(n_live, 1):
            action = "analyze"
        elif size_row is not None and size_row["index_size"] is not None:
            n_rows = max(n_live, 1)
            expected = self._expected_index_size_bytes(
                index_kind=index_kind, n_rows=n_rows
            )
            actual = int(size_row["index_size"])
            if actual > expected * 1.5 and actual > 8 * 1024 * 1024:
                action = "reindex"

        if action == "none":
            return MaintenanceReport(
                collection_id=collection_id,
                table_name=table_name,
                action=action,
                n_live_tup=n_live,
                n_dead_tup=n_dead,
                n_mod_since_analyze=n_mod,
                duration_seconds=0.0,
                started_at=started,
                detail=None,
            )

        action_started = time.monotonic()
        detail: str | None = None
        try:
            await self._apply_action(
                action=action,
                table_name=table_name,
                index_name=index_name,
            )
        except Exception as exc:
            detail = f"action {action!r} failed: {exc}"
            logger.exception(
                "maintain_indexes: %s on %s.%s failed",
                action,
                self._schema,
                table_name,
            )
        duration = time.monotonic() - action_started

        return MaintenanceReport(
            collection_id=collection_id,
            table_name=table_name,
            action=action,
            n_live_tup=n_live,
            n_dead_tup=n_dead,
            n_mod_since_analyze=n_mod,
            duration_seconds=duration,
            started_at=started,
            detail=detail,
        )

    def _expected_index_size_bytes(
        self,
        *,
        index_kind: str,
        n_rows: int,
    ) -> int:
        """Estimate the expected ANN index size for ``n_rows``.

        Used as the denominator of the bloat heuristic: when the
        actual index is more than 1.5x larger than expected (and at
        least 8 MiB), maintenance triggers a ``REINDEX``.

        Default formula: HNSW ~ ``n_rows * m * 16`` bytes (graph
        edges). Subclasses override for other index types.
        """
        if index_kind == "hnsw":
            return n_rows * self._config.hnsw_m * 16
        # Unknown kind -- generous estimate so we don't reindex too eagerly.
        return n_rows * 1024

    async def _apply_action(
        self,
        *,
        action: MaintenanceAction,
        table_name: str,
        index_name: str,
    ) -> None:
        qualified_table = f'"{self._schema}"."{table_name}"'
        qualified_index = f'"{self._schema}"."{index_name}"'

        async with self._pool.acquire() as conn:
            if action == "analyze":
                await conn.execute(f"ANALYZE {qualified_table}")
            elif action == "vacuum_analyze":
                await conn.execute(f"VACUUM ANALYZE {qualified_table}")
            elif action == "reindex":
                await conn.execute(
                    f"REINDEX INDEX CONCURRENTLY {qualified_index}"
                )
            else:  # pragma: no cover - exhaustive
                raise ConfigError(f"unknown maintenance action {action!r}")


# ===========================================================================
# VectorStore
# ===========================================================================


class PgVectorStore(VectorStore):
    """pgvector-backed :class:`VectorStore` with per-collection HNSW tables.

    Subclassing hooks for swapping the index type:

    * :meth:`_index_kind` -- short tag stored in the catalogue (``"hnsw"``).
    * :meth:`_index_suffix` -- suffix added to the table name to form
      the index name (``"hnsw"`` -> ``embeddings_foo_hnsw``).
    * :meth:`_render_index_ddl` -- the full ``CREATE INDEX`` statement.

    All three default to HNSW. Subclasses (pgvectorscale + DiskANN)
    override to emit the appropriate DDL while reusing every other
    code path.
    """

    def __init__(self, provider: PgVectorStoreProvider) -> None:
        self._provider = provider
        self._schema = provider.schema
        self._collections: dict[str, _CollectionMeta] = {}

    # ---------- Subclass override hooks ----------------------------------

    def _index_kind(self) -> str:
        """Short tag describing the ANN index type. Stored in catalogue."""
        return "hnsw"

    def _index_suffix(self) -> str:
        """Suffix added to the table name to form the index name."""
        return self._index_kind()

    def _render_index_ddl(
        self,
        *,
        table_name: str,
        index_name: str,
        opclass: str,
    ) -> str:
        """Render the ``CREATE INDEX`` statement for this index kind."""
        cfg = self._provider.config
        return (
            f'CREATE INDEX IF NOT EXISTS "{index_name}" '
            f'ON "{self._schema}"."{table_name}" '
            f'USING hnsw (vector {opclass}) '
            f'WITH (m = {int(cfg.hnsw_m)}, '
            f'ef_construction = {int(cfg.hnsw_ef_construction)})'
        )

    # ---------- Schema management -----------------------------------------

    async def create_collection(
        self,
        collection_id: str,
        *,
        dimensions: int,
        distance: Literal["cosine", "l2", "ip"] = "cosine",
    ) -> None:
        if dimensions < 1:
            raise BadRequestError(
                f"dimensions must be >= 1, got {dimensions}"
            )
        if distance not in _DISTANCE_OPCLASS:
            raise BadRequestError(f"unknown distance {distance!r}")

        table_name = _table_name_for_collection(collection_id)
        index_name = f"{table_name}_{self._index_suffix()}"
        opclass = _DISTANCE_OPCLASS[distance]

        ddl_table = (
            f'CREATE TABLE IF NOT EXISTS "{self._schema}"."{table_name}" ('
            'document_id text NOT NULL, '
            'chunk_id text NOT NULL, '
            'text text NOT NULL, '
            f'vector vector({dimensions}) NOT NULL, '
            "meta jsonb NOT NULL DEFAULT '{}'::jsonb, "
            'PRIMARY KEY (document_id, chunk_id)'
            ')'
        )
        ddl_doc_index = (
            f'CREATE INDEX IF NOT EXISTS "{table_name}_document" '
            f'ON "{self._schema}"."{table_name}" (document_id)'
        )
        ddl_meta_index = (
            f'CREATE INDEX IF NOT EXISTS "{table_name}_meta_gin" '
            f'ON "{self._schema}"."{table_name}" '
            f'USING gin (meta jsonb_path_ops)'
        )
        ddl_ann = self._render_index_ddl(
            table_name=table_name,
            index_name=index_name,
            opclass=opclass,
        )

        try:
            async with self._provider.pool.acquire() as conn:
                # Catalogue lookup first to surface ConflictError on
                # dimension/distance drift before we attempt DDL.
                existing = await conn.fetchrow(
                    f'SELECT dimensions, distance FROM '
                    f'"{self._schema}".matrix_collections '
                    f'WHERE collection_id = $1',
                    collection_id,
                )
                if existing is not None:
                    if (
                        int(existing["dimensions"]) != dimensions
                        or existing["distance"] != distance
                    ):
                        raise ConflictError(
                            f"collection {collection_id!r} already exists with "
                            f"dimensions={existing['dimensions']}, "
                            f"distance={existing['distance']!r}; "
                            f"requested dimensions={dimensions}, "
                            f"distance={distance!r}"
                        )
                    self._collections[collection_id] = _CollectionMeta(
                        table_name=table_name,
                        index_name=index_name,
                        dimensions=dimensions,
                        distance=distance,
                    )
                    return

                async with conn.transaction():
                    await conn.execute(ddl_table)
                    await conn.execute(ddl_doc_index)
                    await conn.execute(ddl_meta_index)
                    await conn.execute(ddl_ann)
                    await conn.execute(
                        f'INSERT INTO "{self._schema}".matrix_collections '
                        f'(collection_id, table_name, index_name, '
                        f'index_kind, dimensions, distance) '
                        f'VALUES ($1, $2, $3, $4, $5, $6)',
                        collection_id,
                        table_name,
                        index_name,
                        self._index_kind(),
                        dimensions,
                        distance,
                    )
        except ConflictError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"failed to create collection {collection_id!r}: {exc}",
                cause=exc,
            ) from exc

        self._collections[collection_id] = _CollectionMeta(
            table_name=table_name,
            index_name=index_name,
            dimensions=dimensions,
            distance=distance,
        )
        logger.info(
            "Created vector collection %r (dimensions=%d, distance=%s, "
            "index_kind=%s, table=%s)",
            collection_id,
            dimensions,
            distance,
            self._index_kind(),
            table_name,
        )

    async def _resolve(self, collection_id: str) -> _CollectionMeta:
        cached = self._collections.get(collection_id)
        if cached is not None:
            return cached
        async with self._provider.pool.acquire() as conn:
            row = await conn.fetchrow(
                f'SELECT table_name, index_name, dimensions, distance FROM '
                f'"{self._schema}".matrix_collections WHERE collection_id = $1',
                collection_id,
            )
        if row is None:
            raise BadRequestError(
                f"collection {collection_id!r} has not been created"
            )
        meta = _CollectionMeta(
            table_name=row["table_name"],
            index_name=row["index_name"],
            dimensions=int(row["dimensions"]),
            distance=row["distance"],
        )
        self._collections[collection_id] = meta
        return meta

    # ---------- put / search / search_by_meta / get / delete --------------

    async def put(self, record: EmbeddingRecord) -> None:
        meta = await self._resolve(record.collection_id)
        if len(record.vector) != meta.dimensions:
            raise BadRequestError(
                f"vector dimensionality {len(record.vector)} does not match "
                f"collection {record.collection_id!r} dimensions={meta.dimensions}"
            )

        sql = (
            f'INSERT INTO "{self._schema}"."{meta.table_name}" '
            f'(document_id, chunk_id, text, vector, meta) '
            f'VALUES ($1, $2, $3, $4, $5::jsonb) '
            f'ON CONFLICT (document_id, chunk_id) DO UPDATE SET '
            f'text = EXCLUDED.text, '
            f'vector = EXCLUDED.vector, '
            f'meta = EXCLUDED.meta'
        )
        try:
            async with self._provider.pool.acquire() as conn:
                await conn.execute(
                    sql,
                    record.document_id,
                    record.chunk_id,
                    record.text,
                    record.vector,
                    json.dumps(record.meta),
                )
        except Exception as exc:
            raise self._wrap_db_error(exc) from exc

    async def search(
        self,
        collection_id: str,
        vector: Vector,
        k: int,
    ) -> list[SearchResult]:
        if k < 1:
            raise BadRequestError(f"k must be >= 1, got {k}")

        meta = await self._resolve(collection_id)
        if len(vector) != meta.dimensions:
            raise BadRequestError(
                f"query vector dimensionality {len(vector)} does not match "
                f"collection {collection_id!r} dimensions={meta.dimensions}"
            )

        op = _DISTANCE_OPERATOR[meta.distance]
        # Normalise to "higher = more similar" regardless of native metric.
        if meta.distance == "ip":
            # pgvector's <#> returns negative dot product (lower better);
            # flip sign to expose a similarity score.
            score_expr = f"-(vector {op} $1)"
        else:
            # Cosine distance and L2 -- bounded similarity 1 / (1 + d).
            score_expr = f"1 / (1 + (vector {op} $1))"

        sql = (
            f'SELECT document_id, chunk_id, text, vector, meta, '
            f'{score_expr} AS score '
            f'FROM "{self._schema}"."{meta.table_name}" '
            f'ORDER BY vector {op} $1 '
            f'LIMIT $2'
        )

        try:
            async with self._provider.pool.acquire() as conn:
                rows = await conn.fetch(sql, vector, k)
        except Exception as exc:
            raise self._wrap_db_error(exc) from exc

        return [
            SearchResult(
                record=EmbeddingRecord(
                    collection_id=collection_id,
                    document_id=r["document_id"],
                    chunk_id=r["chunk_id"],
                    text=r["text"],
                    vector=list(r["vector"]),
                    meta=_meta_from_json(r["meta"]),
                ),
                score=float(r["score"]) if r["score"] is not None else None,
            )
            for r in rows
        ]

    async def search_by_meta(
        self,
        collection_id: str,
        meta: dict[str, Any],
    ) -> list[EmbeddingRecord]:
        col = await self._resolve(collection_id)
        # Containment query: ``meta @> $1`` returns rows whose meta
        # JSON contains the supplied JSON. The GIN index on meta with
        # the jsonb_path_ops opclass makes this index-supported.
        sql = (
            f'SELECT document_id, chunk_id, text, vector, meta '
            f'FROM "{self._schema}"."{col.table_name}" '
            f'WHERE meta @> $1::jsonb '
            f'ORDER BY document_id ASC, chunk_id ASC'
        )
        try:
            async with self._provider.pool.acquire() as conn:
                rows = await conn.fetch(sql, json.dumps(meta))
        except Exception as exc:
            raise self._wrap_db_error(exc) from exc

        return [
            EmbeddingRecord(
                collection_id=collection_id,
                document_id=r["document_id"],
                chunk_id=r["chunk_id"],
                text=r["text"],
                vector=list(r["vector"]),
                meta=_meta_from_json(r["meta"]),
            )
            for r in rows
        ]

    async def get(
        self,
        collection_id: str,
        document_id: str,
    ) -> list[EmbeddingRecord]:
        meta = await self._resolve(collection_id)
        sql = (
            f'SELECT document_id, chunk_id, text, vector, meta '
            f'FROM "{self._schema}"."{meta.table_name}" '
            f'WHERE document_id = $1 ORDER BY chunk_id ASC'
        )
        try:
            async with self._provider.pool.acquire() as conn:
                rows = await conn.fetch(sql, document_id)
        except Exception as exc:
            raise self._wrap_db_error(exc) from exc

        return [
            EmbeddingRecord(
                collection_id=collection_id,
                document_id=r["document_id"],
                chunk_id=r["chunk_id"],
                text=r["text"],
                vector=list(r["vector"]),
                meta=_meta_from_json(r["meta"]),
            )
            for r in rows
        ]

    async def delete(
        self,
        collection_id: str,
        document_id: str,
    ) -> None:
        # Per the VectorStore contract, delete is idempotent. If the
        # collection doesn't exist we still return cleanly.
        try:
            meta = await self._resolve(collection_id)
        except BadRequestError:
            return
        sql = (
            f'DELETE FROM "{self._schema}"."{meta.table_name}" '
            f'WHERE document_id = $1'
        )
        try:
            async with self._provider.pool.acquire() as conn:
                await conn.execute(sql, document_id)
        except Exception as exc:
            raise self._wrap_db_error(exc) from exc

    # ---------- helpers ---------------------------------------------------

    def _wrap_db_error(self, exc: Exception) -> Exception:
        if isinstance(exc, asyncpg.PostgresError):
            return ServerError(f"pgvector error: {exc}", cause=exc)
        return ProviderError(f"vector store backend error: {exc}", cause=exc)


# ===========================================================================
# Internal types
# ===========================================================================


class _CollectionMeta:
    __slots__ = ("table_name", "index_name", "dimensions", "distance")

    def __init__(
        self,
        *,
        table_name: str,
        index_name: str,
        dimensions: int,
        distance: str,
    ) -> None:
        self.table_name = table_name
        self.index_name = index_name
        self.dimensions = dimensions
        self.distance = distance


def _meta_from_json(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    return json.loads(value)
