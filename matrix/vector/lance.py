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
open-ended object schemas. ``search_by_meta`` builds its predicate
using LanceDB's SQL ``json_extract``.
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

    async def maintain_indexes(self) -> list[MaintenanceReport]:
        # Catalogue + per-table optimize. Stubbed in Task 6.
        raise NotImplementedError  # filled in Task 6


class LanceVectorStore(VectorStore):
    """LanceDB-backed :class:`VectorStore`. Implemented across Tasks 4-6."""

    def __init__(self, *, provider: LanceVectorStoreProvider) -> None:
        self._provider = provider

    # All abstract methods filled in subsequent tasks. Provide stubs so
    # the file imports cleanly; tests for each method live in the same
    # task that fills it in.

    async def create_collection(self, *args, **kwargs):
        raise NotImplementedError  # Task 4

    async def put(self, *args, **kwargs):
        raise NotImplementedError  # Task 5

    async def search(self, *args, **kwargs):
        raise NotImplementedError  # Task 5

    async def search_by_meta(self, *args, **kwargs):
        raise NotImplementedError  # Task 5

    async def get(self, *args, **kwargs):
        raise NotImplementedError  # Task 5

    async def delete(self, *args, **kwargs):
        raise NotImplementedError  # Task 5
