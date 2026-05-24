"""SQLite-backed :class:`StorageProvider` and :class:`Storage[T]`.

Embedded single-file backend. aiosqlite serialises all SQL through
its background thread; one connection per provider is sufficient.
WAL mode (the default) allows external readers (e.g. a ``sqlite3``
shell) without blocking the application's writes.

Schema layout mirrors the Postgres backend:

.. code-block:: sql

    CREATE TABLE IF NOT EXISTS <table> (
        id          TEXT PRIMARY KEY,
        data        TEXT NOT NULL,          -- JSON-encoded
        created_at  TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );

Per-model tables are created lazily on first use (same as Postgres).
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, TypeVar

import aiosqlite
from pydantic import BaseModel

from matrix.int.storage import Storage
from matrix.int.storage_provider import StorageProvider
from matrix.model.common import Identifiable
from matrix.model.except_ import ConfigError, ProviderError
from matrix.model.provider import SqliteConfig


logger = logging.getLogger(__name__)


ModelT = TypeVar("ModelT", bound=Identifiable)


def _table_name_for(model_class: type[BaseModel]) -> str:
    """Derive a table name from a model class.

    Mirrors :func:`matrix.storage.postgres._table_name_for` exactly so
    the two backends place each model in the same-named table; an
    operator can swap providers without renaming anything.
    """
    name = model_class.__name__.lower()
    if name == "session":
        return "sessions"
    return name


class SqliteStorageProvider(StorageProvider):
    """Storage provider backed by a single embedded SQLite file."""

    def __init__(self, config: SqliteConfig) -> None:
        self._config = config
        self._conn: aiosqlite.Connection | None = None
        self._handles: dict[type[Identifiable], SqliteStorage[Any]] = {}

    @property
    def connection(self) -> aiosqlite.Connection:
        """The shared aiosqlite connection. Raises if not initialised."""
        if self._conn is None:
            raise ConfigError(
                "SqliteStorageProvider used before initialize()"
            )
        return self._conn

    async def initialize(self) -> None:
        if self._conn is not None:
            return
        path = self._config.path.expanduser()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            raise ProviderError(
                f"failed to create SQLite parent directory {path.parent}: {exc}",
                cause=exc,
            ) from exc
        try:
            self._conn = await aiosqlite.connect(str(path))
            await self._conn.execute(
                f"PRAGMA journal_mode = {self._config.journal_mode}"
            )
            await self._conn.execute(
                f"PRAGMA synchronous = {self._config.synchronous}"
            )
            await self._conn.execute(
                f"PRAGMA busy_timeout = {self._config.busy_timeout_ms}"
            )
            await self._conn.execute("PRAGMA foreign_keys = ON")
            await self._conn.commit()
        except Exception as exc:
            # Roll back partial open on failure.
            if self._conn is not None:
                try:
                    await self._conn.close()
                except Exception:
                    logger.exception("aiosqlite close after init failure")
                self._conn = None
            raise ProviderError(
                f"failed to open SQLite database at {path}: {exc}",
                cause=exc,
            ) from exc
        logger.info(
            "SqliteStorageProvider initialised (path=%s, journal=%s)",
            path, self._config.journal_mode,
        )

    async def aclose(self) -> None:
        if self._conn is None:
            return
        try:
            await self._conn.close()
        finally:
            self._conn = None
            self._handles.clear()
        logger.info("SqliteStorageProvider closed")

    def get_storage(self, model_class: type[ModelT]) -> Storage[ModelT]:
        cached = self._handles.get(model_class)
        if cached is not None:
            return cached
        handle = SqliteStorage[ModelT](provider=self, model_class=model_class)
        self._handles[model_class] = handle
        return handle


class SqliteStorage(Storage[ModelT]):
    """Per-model SQLite-backed :class:`Storage` handle.

    Lifecycle methods are stubbed in this task; CRUD lands in Task 5
    and list/find in Task 6.
    """

    def __init__(
        self,
        provider: SqliteStorageProvider,
        model_class: type[ModelT],
    ) -> None:
        self._provider = provider
        self._model = model_class
        self._table = _table_name_for(model_class)

    async def get(self, id: str):  # noqa: A002 -- ABC signature
        # Touching the provider triggers ConfigError if not initialised.
        _ = self._provider.connection
        raise NotImplementedError("filled in Task 5")

    async def create(self, entity):
        _ = self._provider.connection
        raise NotImplementedError("filled in Task 5")

    async def update(self, entity):
        _ = self._provider.connection
        raise NotImplementedError("filled in Task 5")

    async def delete(self, id: str) -> None:  # noqa: A002
        _ = self._provider.connection
        raise NotImplementedError("filled in Task 5")

    async def list(self, page, *, order_by=None):
        _ = self._provider.connection
        raise NotImplementedError("filled in Task 6")

    async def find(self, predicate, page, *, order_by=None):
        _ = self._provider.connection
        raise NotImplementedError("filled in Task 6")


# Map sqlite3 exception classes onto matrix domain exceptions.
# Used by CRUD methods in Task 5.
def _wrap_sqlite_error(exc: Exception, *, model_name: str, op: str) -> Exception:
    """Translate sqlite3 errors into matrix domain exceptions."""
    from matrix.model.except_ import ConflictError, ServerError  # local: cycle-proof

    if isinstance(exc, sqlite3.IntegrityError):
        # UNIQUE violation surfaces as IntegrityError; the message
        # contains "UNIQUE constraint failed".
        if "UNIQUE constraint failed" in str(exc):
            return ConflictError(
                f"{model_name} id conflict during {op}: {exc}", cause=exc,
            )
        return ProviderError(
            f"{model_name} integrity error during {op}: {exc}", cause=exc,
        )
    if isinstance(exc, sqlite3.OperationalError):
        return ServerError(
            f"SQLite operational error during {model_name}.{op}: {exc}",
            cause=exc,
        )
    if isinstance(exc, sqlite3.DatabaseError):
        return ProviderError(
            f"SQLite database error during {model_name}.{op}: {exc}",
            cause=exc,
        )
    return ProviderError(
        f"SQLite backend error during {model_name}.{op}: {exc}",
        cause=exc,
    )


__all__ = ["SqliteStorage", "SqliteStorageProvider"]
