"""LanceDB-backed :class:`VectorStoreProvider` and :class:`VectorStore`.

Embedded-mode vector store: one ``lancedb.AsyncConnection`` per
provider, one Lance table per collection. The provider owns a
catalogue (``_matrix_collections``) that tracks every collection's
table name, dimensions, distance metric, and whether an ANN index
has been built — so :meth:`maintain_indexes` can enumerate managed
tables without scanning the filesystem and so collection metadata
survives restart.

Search is brute-force until a collection accumulates
``LanceConfig.index_min_rows`` rows; at that point :meth:`put` lazily
triggers ``create_index`` (one-shot, idempotent) and updates the
catalogue. The HNSW index variant LanceDB exposes is ``IVF_HNSW_SQ``.

Storage layout: ``meta`` is persisted as a JSON-serialised utf8
column rather than a struct because Arrow cannot represent
open-ended object schemas. ``search_by_meta`` deserialises every
candidate row and matches in Python (LanceDB 0.30.2's SQL
``json_extract`` requires a ``LargeBinary`` column type that our
utf8 layout doesn't satisfy; the Python-side fallback preserves
contract semantics at the cost of a linear scan).
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

import lancedb
import pyarrow as pa

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
from matrix.model.provider import LanceConfig
from matrix.model.vector import EmbeddingRecord, SearchResult, Vector


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _similarity(distance_metric: str, raw_distance: float) -> float:
    """Map LanceDB's distance value to a similarity score per the
    VectorStore convention (higher = more similar)."""
    if distance_metric == "cosine":
        # LanceDB cosine returns 1 - cosine_similarity in [0, 2].
        return 1.0 - float(raw_distance)
    if distance_metric == "l2":
        return 1.0 / (1.0 + float(raw_distance))
    if distance_metric == "dot":
        # dot returns the negated inner product (lower = better).
        return -float(raw_distance)
    return float(raw_distance)


def _meta_predicate(meta: dict[str, Any]) -> str | None:
    """Build a SQL filter expression that matches ``meta``.

    NOTE: LanceDB 0.30.2's ``json_extract`` only accepts ``LargeBinary``
    columns, which is incompatible with the ``utf8`` schema used for
    ``meta``. This function returns ``None`` unconditionally so that
    ``search_by_meta`` fetches all rows and applies the predicate in
    Python. The helper is kept for API completeness and future-compat.
    """
    return None  # client-side filtering applied in search_by_meta


def _walk_meta(value: Any, *, path: str, out: list[str]) -> None:
    """Recursively collect leaf-level match clauses for ``value``."""
    if isinstance(value, dict):
        for k, v in value.items():
            _walk_meta(v, path=f"{path}.{k}", out=out)
        return
    if isinstance(value, str):
        escaped = value.replace("'", "''")
        out.append(f"json_extract(meta, '{path}') = '{escaped}'")
    elif isinstance(value, bool):
        out.append(f"json_extract(meta, '{path}') = {str(value).lower()}")
    elif isinstance(value, (int, float)):
        out.append(f"CAST(json_extract(meta, '{path}') AS DOUBLE) = {value}")
    elif value is None:
        out.append(f"json_extract(meta, '{path}') IS NULL")
    else:
        encoded = json.dumps(value).replace("'", "''")
        out.append(f"json_extract(meta, '{path}') = '{encoded}'")


def _meta_matches(row_meta_json: str, filter_meta: dict[str, Any]) -> bool:
    """Return True if ``row_meta_json`` (JSON string) contains every
    key/value pair in ``filter_meta`` (supports nested dicts)."""
    if not filter_meta:
        return True
    try:
        row_meta = json.loads(row_meta_json) if row_meta_json else {}
    except (json.JSONDecodeError, TypeError):
        return False
    return _meta_deep_match(row_meta, filter_meta)


def _meta_deep_match(row: Any, pattern: Any) -> bool:
    """Recursively check that every key in ``pattern`` matches ``row``."""
    if isinstance(pattern, dict):
        if not isinstance(row, dict):
            return False
        return all(
            k in row and _meta_deep_match(row[k], v)
            for k, v in pattern.items()
        )
    return row == pattern


def _escape_sql_string(value: str) -> str:
    """Escape a string for safe single-quoted SQL literal use.

    LanceDB's filter API does not (as of 0.30.2) expose parameterised
    queries on the AsyncTable surface — only inline SQL expressions.
    Use SQL-standard single-quote doubling to neutralise apostrophes
    in user-supplied identifiers like document_id.
    """
    return value.replace("'", "''")


_COLLECTION_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_CATALOGUE_TABLE = "_matrix_collections"

# Map the VectorStore ABC's distance vocabulary to LanceDB's.
_DISTANCE_LANCE: dict[str, str] = {
    "cosine": "cosine",
    "l2": "l2",
    "ip": "dot",
}


def _table_name_for_collection(collection_id: str) -> str:
    """Map a collection id to a Lance table name. Same convention as pgvector."""
    if not _COLLECTION_ID_RE.match(collection_id):
        raise BadRequestError(
            f"collection_id {collection_id!r} must match "
            f"{_COLLECTION_ID_RE.pattern} (alphanumeric / underscore / hyphen)"
        )
    return "embeddings_" + collection_id.replace("-", "_")


def _catalogue_schema() -> pa.Schema:
    return pa.schema([
        pa.field("collection_id", pa.utf8(), nullable=False),
        pa.field("table_name", pa.utf8(), nullable=False),
        pa.field("dimensions", pa.int32(), nullable=False),
        pa.field("distance", pa.utf8(), nullable=False),
        pa.field("indexed", pa.bool_(), nullable=False),
    ])


def _record_schema(dimensions: int) -> pa.Schema:
    return pa.schema([
        pa.field("document_id", pa.utf8(), nullable=False),
        pa.field("chunk_id", pa.utf8(), nullable=False),
        pa.field("text", pa.utf8(), nullable=False),
        pa.field("vector", pa.list_(pa.float32(), dimensions), nullable=False),
        pa.field("meta", pa.utf8(), nullable=False),  # JSON-serialised
    ])


class LanceVectorStoreProvider(VectorStoreProvider):
    """LanceDB embedded :class:`VectorStoreProvider`.

    One ``lancedb.AsyncConnection`` per provider; one Lance table per
    collection. Persistent across restart via the on-disk Lance format.
    """

    def __init__(self, config: LanceConfig) -> None:
        self._config = config
        self._db: lancedb.AsyncConnection | None = None
        self._store: "LanceVectorStore | None" = None

    @property
    def config(self) -> LanceConfig:
        return self._config

    @property
    def db(self) -> "lancedb.AsyncConnection":
        if self._db is None:
            raise ConfigError(
                f"{type(self).__name__} used before initialize()"
            )
        return self._db

    async def initialize(self) -> None:
        if self._db is not None:
            return
        try:
            self._config.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise ConfigError(
                f"failed to create LanceDB path {self._config.path}: {exc}"
            ) from exc
        try:
            self._db = await lancedb.connect_async(str(self._config.path))
        except Exception as exc:
            raise ProviderError(
                f"failed to open LanceDB at {self._config.path}: {exc}",
                cause=exc,
            ) from exc
        logger.info(
            "%s initialised at %s",
            type(self).__name__,
            self._config.path,
        )

    async def aclose(self) -> None:
        if self._db is None:
            return
        try:
            # AsyncConnection has no explicit close() in current lancedb;
            # dropping the reference closes the underlying handles.
            close_coro = getattr(self._db, "close", None)
            if close_coro is not None:
                result = close_coro()
                if hasattr(result, "__await__"):
                    await result
        except Exception as exc:  # noqa: BLE001 - best-effort
            logger.warning("LanceVectorStoreProvider.aclose: %s", exc)
        self._db = None
        self._store = None
        logger.info("%s closed", type(self).__name__)

    def get_vector_store(self) -> VectorStore:
        if self._store is None:
            from matrix.vector.lance import LanceVectorStore  # self-import
            self._store = LanceVectorStore(provider=self)
        return self._store

    # ---------- Catalogue helpers -----------------------------------------

    async def _open_catalogue(self) -> "lancedb.AsyncTable":
        """Open (or create) the catalogue table. Idempotent."""
        response = await self.db.list_tables()
        names = response.tables
        if _CATALOGUE_TABLE in names:
            return await self.db.open_table(_CATALOGUE_TABLE)
        # AsyncConnection.create_table accepts either `data` (any
        # Arrow-compatible payload) or `schema` (when the table is
        # initially empty). Passing the schema avoids constructing an
        # empty pyarrow.Table.
        return await self.db.create_table(
            _CATALOGUE_TABLE, schema=_catalogue_schema()
        )

    async def _read_catalogue(self) -> list[dict[str, Any]]:
        """Return every catalogue row, ordered by collection_id."""
        tbl = await self._open_catalogue()
        rows = await tbl.query().to_list()
        rows.sort(key=lambda r: r["collection_id"])
        return rows

    async def _catalogue_get(self, collection_id: str) -> dict[str, Any] | None:
        tbl = await self._open_catalogue()
        rows = await tbl.query().where(
            f"collection_id = '{_escape_sql_string(collection_id)}'"
        ).to_list()
        return rows[0] if rows else None

    async def _catalogue_insert(
        self,
        *,
        collection_id: str,
        table_name: str,
        dimensions: int,
        distance: str,
    ) -> None:
        tbl = await self._open_catalogue()
        row = {
            "collection_id": collection_id,
            "table_name": table_name,
            "dimensions": int(dimensions),
            "distance": distance,
            "indexed": False,
        }
        await tbl.add([row])

    async def _catalogue_mark_indexed(self, collection_id: str) -> None:
        tbl = await self._open_catalogue()
        # AsyncTable.update signature on lancedb 0.15+:
        # `update(updates: dict, where: str | None = None)`.
        await tbl.update(
            updates={"indexed": True},
            where=f"collection_id = '{_escape_sql_string(collection_id)}'",
        )

    async def maintain_indexes(self) -> list[MaintenanceReport]:
        if self._db is None:
            raise ConfigError(
                f"{type(self).__name__}.maintain_indexes called before initialize()"
            )

        reports: list[MaintenanceReport] = []
        catalogue = await self._read_catalogue()
        for row in catalogue:
            collection_id = row["collection_id"]
            table_name = row["table_name"]
            indexed = bool(row.get("indexed"))
            started = datetime.now(timezone.utc)
            t0 = time.monotonic()
            detail: str | None = None
            action: MaintenanceAction = "reindex" if indexed else "none"
            n_live: int | None = None
            try:
                table = await self._db.open_table(table_name)
                try:
                    n_live = await table.count_rows()
                except Exception:
                    n_live = None
                if indexed:
                    # LanceDB's optimize() combines compaction + index
                    # rewrite. Fast on small tables, idempotent.
                    # On lancedb 0.30.2 AsyncTable.optimize() is a coroutine.
                    try:
                        await table.optimize()
                    except Exception as opt_exc:  # noqa: BLE001 - best-effort
                        detail = f"optimize failed: {opt_exc}"
                        action = "none"
            except Exception as exc:  # noqa: BLE001 - per-table best-effort
                action = "none"
                detail = f"maintain failed: {exc}"
            duration = time.monotonic() - t0
            reports.append(MaintenanceReport(
                collection_id=collection_id,
                table_name=table_name,
                action=action,
                n_live_tup=n_live,
                n_dead_tup=None,
                n_mod_since_analyze=None,
                duration_seconds=duration,
                started_at=started,
                detail=detail,
            ))
        return reports


class LanceVectorStore(VectorStore):
    """LanceDB-backed :class:`VectorStore`. Implemented across Tasks 4-6."""

    def __init__(self, *, provider: LanceVectorStoreProvider) -> None:
        self._provider = provider

    # ---------- Internal helpers -----------------------------------------

    async def _open_table(self, collection_id: str) -> "lancedb.AsyncTable":
        """Resolve the per-collection Lance table; BadRequestError when
        the collection has never been created."""
        cat = await self._provider._catalogue_get(collection_id)
        if cat is None:
            raise BadRequestError(
                f"collection {collection_id!r} is not registered on this "
                f"SemanticSearchProvider"
            )
        return await self._provider.db.open_table(cat["table_name"])

    @staticmethod
    def _row_to_record(row: dict[str, Any], collection_id: str) -> EmbeddingRecord:
        return EmbeddingRecord(
            collection_id=collection_id,
            document_id=row["document_id"],
            chunk_id=row["chunk_id"],
            text=row["text"],
            vector=list(row["vector"]),
            meta=json.loads(row["meta"]) if row.get("meta") else {},
        )

    async def _build_index(
        self,
        *,
        table: "lancedb.AsyncTable",
        collection_id: str,
        distance_metric: str,
    ) -> None:
        """Build the best available ANN index, falling back through
        available types if a given type is rejected by this LanceDB build.

        LanceDB 0.30.2 API: ``create_index(column, *, config=<IndexConfig>)``
        where ``column`` is the vector column name and ``config`` is an
        instance from ``lancedb.index`` (HnswSq, HnswPq, IvfPq, …).

        ``IVF_HNSW_SQ`` from the plan corresponds to ``HnswSq`` here;
        ``IVF_PQ`` corresponds to ``IvfPq``.
        """
        from lancedb.index import HnswSq, HnswPq, IvfPq  # local import — optional dep

        # Map our distance vocabulary to what lancedb.index expects.
        # lancedb accepts "l2", "cosine", "dot".
        dist = distance_metric  # already in lancedb's vocabulary
        cfg = self._provider.config

        # HnswSq and HnswPq both accept m= and ef_construction= kwargs
        # (confirmed on lancedb 0.30.2). IvfPq does not expose these params.
        index_configs = [
            HnswSq(
                distance_type=dist,
                num_partitions=1,
                m=int(cfg.hnsw_m),
                ef_construction=int(cfg.hnsw_ef_construction),
            ),  # modern HNSW variant
            HnswPq(
                distance_type=dist,
                num_partitions=1,
                num_sub_vectors=1,
                m=int(cfg.hnsw_m),
                ef_construction=int(cfg.hnsw_ef_construction),
            ),
            IvfPq(distance_type=dist, num_partitions=1, num_sub_vectors=1),
        ]
        for cfg in index_configs:
            try:
                await table.create_index("vector", config=cfg)
                break
            except Exception as exc:
                logger.debug(
                    "LanceDB create_index(%s) on %s failed: %s",
                    type(cfg).__name__, collection_id, exc,
                )
                continue
        else:
            # Every index type failed; log + leave indexed=false so we
            # retry on the next put past the threshold.
            logger.warning(
                "LanceDB index build failed on %s; will retry", collection_id
            )
            return
        await self._provider._catalogue_mark_indexed(collection_id)

    # All abstract methods filled in subsequent tasks. Provide stubs so
    # the file imports cleanly; tests for each method live in the same
    # task that fills it in.

    async def create_collection(
        self,
        collection_id: str,
        *,
        dimensions: int,
        distance: str = "cosine",
    ) -> None:
        if distance not in _DISTANCE_LANCE:
            raise BadRequestError(
                f"unsupported distance {distance!r}; expected one of "
                f"{sorted(_DISTANCE_LANCE)}"
            )
        lance_distance = _DISTANCE_LANCE[distance]
        table_name = _table_name_for_collection(collection_id)

        existing = await self._provider._catalogue_get(collection_id)
        if existing is not None:
            if int(existing["dimensions"]) != int(dimensions):
                raise ConflictError(
                    f"collection {collection_id!r} already exists with "
                    f"dimensions={existing['dimensions']}; cannot recreate "
                    f"with dimensions={dimensions}"
                )
            if existing["distance"] != lance_distance:
                raise ConflictError(
                    f"collection {collection_id!r} already exists with "
                    f"distance={existing['distance']!r}; cannot recreate "
                    f"with distance={lance_distance!r}"
                )
            return  # idempotent no-op

        # Create the per-collection table from the bare schema. Lance
        # materialises an empty dataset; rows arrive via put().
        schema = _record_schema(dimensions)
        try:
            await self._provider.db.create_table(table_name, schema=schema)
        except Exception as exc:
            raise ProviderError(
                f"failed to create Lance table {table_name!r}: {exc}",
                cause=exc,
            ) from exc

        await self._provider._catalogue_insert(
            collection_id=collection_id,
            table_name=table_name,
            dimensions=dimensions,
            distance=lance_distance,
        )

    async def put(self, record: EmbeddingRecord) -> None:
        table = await self._open_table(record.collection_id)
        cat = await self._provider._catalogue_get(record.collection_id)
        assert cat is not None  # _open_table would have raised
        expected_dim = int(cat["dimensions"])
        if len(record.vector) != expected_dim:
            raise BadRequestError(
                f"vector length {len(record.vector)} does not match "
                f"collection {record.collection_id!r} dimensions={expected_dim}"
            )
        row = {
            "document_id": record.document_id,
            "chunk_id": record.chunk_id,
            "text": record.text,
            "vector": [float(x) for x in record.vector],
            "meta": json.dumps(record.meta or {}, sort_keys=True),
        }
        try:
            await (
                table.merge_insert(["document_id", "chunk_id"])
                .when_matched_update_all()
                .when_not_matched_insert_all()
                .execute([row])
            )
        except Exception as exc:
            raise ProviderError(
                f"LanceDB put failed for {record.collection_id}/"
                f"{record.document_id}/{record.chunk_id}: {exc}",
                cause=exc,
            ) from exc

        # Lazy ANN-index build: build once when row-count crosses
        # config.index_min_rows. Idempotent at the catalogue level via
        # the `indexed` flag.
        if not cat.get("indexed", False):
            try:
                row_count = await table.count_rows()
            except Exception:
                row_count = 0
            threshold = int(self._provider.config.index_min_rows)
            if row_count >= threshold:
                await self._build_index(
                    table=table,
                    collection_id=record.collection_id,
                    distance_metric=cat["distance"],
                )

    async def get(
        self,
        collection_id: str,
        document_id: str,
    ) -> list[EmbeddingRecord]:
        table = await self._open_table(collection_id)
        rows = await (
            table.query()
            .where(f"document_id = '{_escape_sql_string(document_id)}'")
            .to_list()
        )
        rows.sort(key=lambda r: r["chunk_id"])
        return [self._row_to_record(r, collection_id) for r in rows]

    async def delete(
        self,
        collection_id: str,
        document_id: str,
    ) -> None:
        table = await self._open_table(collection_id)
        try:
            await table.delete(f"document_id = '{_escape_sql_string(document_id)}'")
        except Exception as exc:
            raise ProviderError(
                f"LanceDB delete failed for {collection_id}/{document_id}: {exc}",
                cause=exc,
            ) from exc

    async def search(
        self,
        collection_id: str,
        vector: Vector,
        k: int,
    ) -> list[SearchResult]:
        table = await self._open_table(collection_id)
        cat = await self._provider._catalogue_get(collection_id)
        assert cat is not None
        expected_dim = int(cat["dimensions"])
        if len(vector) != expected_dim:
            raise BadRequestError(
                f"query vector length {len(vector)} does not match "
                f"collection {collection_id!r} dimensions={expected_dim}"
            )
        try:
            # NOTE: table.search() in lancedb 0.30.2 is async (a coroutine);
            # table.vector_search() is synchronous and returns a builder
            # directly. Use vector_search() to avoid needing two awaits.
            rows = await (
                table.vector_search([float(x) for x in vector])
                .limit(int(k))
                .ef(int(self._provider.config.hnsw_ef_search))
                .to_list()
            )
        except Exception as exc:
            raise ProviderError(
                f"LanceDB search failed in {collection_id}: {exc}",
                cause=exc,
            ) from exc

        distance_metric = cat["distance"]
        results: list[SearchResult] = []
        for row in rows:
            record = self._row_to_record(row, collection_id)
            raw = row.get("_distance")
            score = _similarity(distance_metric, raw) if raw is not None else None
            results.append(SearchResult(record=record, score=score))
        return results

    async def search_by_meta(
        self,
        collection_id: str,
        meta: dict[str, Any],
    ) -> list[EmbeddingRecord]:
        table = await self._open_table(collection_id)
        # NOTE: LanceDB 0.30.2's json_extract() only accepts LargeBinary
        # columns; the 'meta' column is utf8. SQL-level filtering is
        # therefore not available. We fetch all rows and filter in Python.
        try:
            rows = await table.query().to_list()
        except Exception as exc:
            raise ProviderError(
                f"LanceDB search_by_meta failed in {collection_id}: {exc}",
                cause=exc,
            ) from exc
        if meta:
            rows = [r for r in rows if _meta_matches(r.get("meta", "{}"), meta)]
        rows.sort(key=lambda r: (r["document_id"], r["chunk_id"]))
        return [self._row_to_record(r, collection_id) for r in rows]
