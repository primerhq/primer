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

import json
import logging
import sqlite3
from typing import Any, TypeVar

import aiosqlite
from pydantic import BaseModel

from matrix.int.storage import Storage
from matrix.int.storage_provider import StorageProvider
from matrix.model.common import Identifiable, dump_for_storage
from matrix.model.except_ import BadRequestError, ConfigError, ConflictError, NotFoundError, ProviderError, ServerError
from matrix.model.provider import SqliteConfig
from matrix.model.storage import (
    CursorPage,
    CursorPageResponse,
    OffsetPage,
    OffsetPageResponse,
    OrderBy,
    PageRequest,
    Predicate,
)
from matrix.storage._cursor import (
    _decode_cursor,
    _encode_cursor_for,
)
from matrix.storage._sqlite_predicate import (
    _SqlitePredicateTranslator,
    _render_field_expr,
    _render_typed_field_expr,
    render_order_by_sqlite,
)


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
    """Per-model :class:`Storage` handle backed by a SQLite JSON-blob table.

    DDL runs lazily on first use; this matches the Postgres backend's
    behaviour and keeps unused models out of the schema.
    """

    def __init__(
        self,
        provider: SqliteStorageProvider,
        model_class: type[ModelT],
    ) -> None:
        self._provider = provider
        self._model = model_class
        self._table = _table_name_for(model_class)
        self._table_ensured = False

    # ----- DDL ----------------------------------------------------------

    async def _ensure_table(self) -> None:
        if self._table_ensured:
            return
        ddl = (
            f'CREATE TABLE IF NOT EXISTS "{self._table}" ('
            "id TEXT PRIMARY KEY, "
            "data TEXT NOT NULL, "
            "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
            "updated_at TEXT NOT NULL DEFAULT (datetime('now'))"
            ")"
        )
        # Access .connection first — propagates ConfigError unchanged if the
        # provider has not been initialised yet.
        conn = self._provider.connection
        try:
            await conn.execute(ddl)
            await conn.commit()
        except Exception as exc:
            raise _wrap_sqlite_error(
                exc, model_name=self._model.__name__, op="ensure_table",
            ) from exc
        self._table_ensured = True

    # ----- serialisation ------------------------------------------------

    def _to_row(self, entity: ModelT) -> tuple[str, str]:
        dumped = dump_for_storage(entity)
        entity_id = dumped.pop("id")
        return entity_id, json.dumps(dumped, separators=(",", ":"))

    def _from_row(self, id_: str, data_json: str) -> ModelT:
        data = json.loads(data_json)
        data["id"] = id_
        return self._model.model_validate(data)

    # ----- CRUD ---------------------------------------------------------

    async def get(self, id: str) -> ModelT | None:  # noqa: A002
        await self._ensure_table()
        sql = f'SELECT id, data FROM "{self._table}" WHERE id = ?'
        try:
            cur = await self._provider.connection.execute(sql, (id,))
            row = await cur.fetchone()
        except Exception as exc:
            raise _wrap_sqlite_error(
                exc, model_name=self._model.__name__, op="get",
            ) from exc
        if row is None:
            return None
        return self._from_row(row[0], row[1])

    async def create(self, entity: ModelT) -> ModelT:
        await self._ensure_table()
        entity_id, data_json = self._to_row(entity)
        sql = (
            f'INSERT INTO "{self._table}" (id, data) VALUES (?, ?) '
            f"RETURNING id, data"
        )
        try:
            cur = await self._provider.connection.execute(
                sql, (entity_id, data_json),
            )
            row = await cur.fetchone()
            await self._provider.connection.commit()
        except Exception as exc:
            raise _wrap_sqlite_error(
                exc, model_name=self._model.__name__, op="create",
            ) from exc
        # RETURNING guarantees a row on success.
        assert row is not None
        return self._from_row(row[0], row[1])

    async def update(self, entity: ModelT) -> ModelT:
        await self._ensure_table()
        entity_id, data_json = self._to_row(entity)
        sql = (
            f'UPDATE "{self._table}" '
            f"SET data = ?, updated_at = datetime('now') "
            f"WHERE id = ? RETURNING id, data"
        )
        try:
            cur = await self._provider.connection.execute(
                sql, (data_json, entity_id),
            )
            row = await cur.fetchone()
            await self._provider.connection.commit()
        except Exception as exc:
            raise _wrap_sqlite_error(
                exc, model_name=self._model.__name__, op="update",
            ) from exc
        if row is None:
            raise NotFoundError(
                f"{self._model.__name__} with id {entity_id!r} not found"
            )
        return self._from_row(row[0], row[1])

    async def delete(self, id: str) -> None:  # noqa: A002
        await self._ensure_table()
        sql = f'DELETE FROM "{self._table}" WHERE id = ?'
        try:
            cur = await self._provider.connection.execute(sql, (id,))
            await self._provider.connection.commit()
            rowcount = cur.rowcount
        except Exception as exc:
            raise _wrap_sqlite_error(
                exc, model_name=self._model.__name__, op="delete",
            ) from exc
        if rowcount == 0:
            raise NotFoundError(
                f"{self._model.__name__} with id {id!r} not found"
            )

    async def list(
        self,
        page: PageRequest,
        *,
        order_by: list[OrderBy] | None = None,
    ) -> OffsetPageResponse[ModelT] | CursorPageResponse[ModelT]:
        return await self._paged(predicate=None, page=page, order_by=order_by)

    async def find(
        self,
        predicate: Predicate | None,
        page: PageRequest,
        *,
        order_by: list[OrderBy] | None = None,
    ) -> OffsetPageResponse[ModelT] | CursorPageResponse[ModelT]:
        return await self._paged(predicate=predicate, page=page, order_by=order_by)

    async def _paged(
        self,
        *,
        predicate: Predicate | None,
        page: PageRequest,
        order_by: list[OrderBy] | None,
    ) -> OffsetPageResponse[ModelT] | CursorPageResponse[ModelT]:
        await self._ensure_table()
        translator = _SqlitePredicateTranslator(self._model)
        where_sql = "1=1"
        if predicate is not None:
            where_sql, _ = translator.translate(predicate)
        order_clause = render_order_by_sqlite(self._model, order_by)
        if isinstance(page, OffsetPage):
            return await self._page_offset(
                translator=translator,
                where_sql=where_sql,
                order_clause=order_clause,
                page=page,
            )
        if isinstance(page, CursorPage):
            return await self._page_cursor(
                translator=translator,
                where_sql=where_sql,
                order_clause=order_clause,
                order_by=order_by,
                page=page,
            )
        raise BadRequestError(
            f"unknown PageRequest variant {type(page).__name__!r}"
        )

    async def _page_offset(
        self,
        *,
        translator: _SqlitePredicateTranslator,
        where_sql: str,
        order_clause: str,
        page: OffsetPage,
    ) -> OffsetPageResponse[ModelT]:
        translator.append_param(page.length)
        translator.append_param(page.offset)
        params = list(translator._params)  # noqa: SLF001
        count_params = params[:-2]
        select_sql = (
            f'SELECT id, data FROM "{self._table}" '
            f"WHERE {where_sql} {order_clause} LIMIT ? OFFSET ?"
        )
        count_sql = (
            f'SELECT count(*) FROM "{self._table}" WHERE {where_sql}'
        )
        try:
            cur = await self._provider.connection.execute(select_sql, params)
            rows = await cur.fetchall()
            cur = await self._provider.connection.execute(count_sql, count_params)
            row = await cur.fetchone()
            total = int(row[0]) if row is not None else None
        except Exception as exc:
            raise _wrap_sqlite_error(
                exc, model_name=self._model.__name__, op="list",
            ) from exc
        items = [self._from_row(r[0], r[1]) for r in rows]
        return OffsetPageResponse[self._model](  # type: ignore[name-defined]
            offset=page.offset,
            length=len(items),
            total=total,
            items=items,
        )

    async def _page_cursor(
        self,
        *,
        translator: _SqlitePredicateTranslator,
        where_sql: str,
        order_clause: str,
        order_by: list[OrderBy] | None,
        page: CursorPage,
    ) -> CursorPageResponse[ModelT]:
        cursor_clause = ""
        if page.cursor is not None:
            cursor_state = _decode_cursor(page.cursor)
            cursor_clause = self._render_cursor_filter(
                translator=translator,
                cursor_state=cursor_state,
            )
        full_where = where_sql
        if cursor_clause:
            full_where = f"({where_sql}) AND ({cursor_clause})"
        translator.append_param(page.length + 1)
        params = list(translator._params)  # noqa: SLF001
        select_sql = (
            f'SELECT id, data FROM "{self._table}" '
            f"WHERE {full_where} {order_clause} LIMIT ?"
        )
        try:
            cur = await self._provider.connection.execute(select_sql, params)
            rows = await cur.fetchall()
        except Exception as exc:
            raise _wrap_sqlite_error(
                exc, model_name=self._model.__name__, op="find",
            ) from exc
        has_more = len(rows) > page.length
        if has_more:
            rows = rows[: page.length]
        items = [self._from_row(r[0], r[1]) for r in rows]
        next_cursor: str | None = None
        if has_more and items:
            next_cursor = _encode_cursor_for(items[-1], order_by)
        return CursorPageResponse[self._model](  # type: ignore[name-defined]
            next_cursor=next_cursor,
            items=items,
        )

    def _render_cursor_filter(
        self,
        *,
        translator: _SqlitePredicateTranslator,
        cursor_state: dict[str, Any],
    ) -> str:
        """Build the WHERE fragment that seeks past the cursor.

        Cursor state shape:
            {"keys": [{"field", "value", "direction"}, ..., {"field": "id", ...}]}
        Lexicographic-expansion seek condition (same shape as the
        Postgres backend; only the cell expressions differ).
        """
        keys = cursor_state.get("keys", [])
        if not keys:
            raise BadRequestError("cursor missing 'keys'")
        clauses: list[str] = []
        for prefix_len in range(len(keys)):
            parts: list[str] = []
            for k in keys[:prefix_len]:
                expr = _render_field_expr(self._model, k["field"])
                ph = translator.append_param(k["value"])
                parts.append(f"({expr} = {ph})")
            k = keys[prefix_len]
            sql_op = ">" if k["direction"] == "asc" else "<"
            if k["field"] == "id":
                left = "id"
            else:
                left = _render_typed_field_expr(self._model, k["field"])
            ph = translator.append_param(k["value"])
            parts.append(f"({left} {sql_op} {ph})")
            clauses.append("(" + " AND ".join(parts) + ")")
        return "(" + " OR ".join(clauses) + ")"


# Map sqlite3 exception classes onto matrix domain exceptions.
# Used by CRUD methods in Task 5.
def _wrap_sqlite_error(exc: Exception, *, model_name: str, op: str) -> Exception:
    """Translate sqlite3 errors into matrix domain exceptions."""

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
