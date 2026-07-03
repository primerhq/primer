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

import asyncio
import json
import logging
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, TypeVar

import aiosqlite
from pydantic import BaseModel

from primer.int.document_content import (
    ContentListEntry,
    ContentRow,
    DocumentContentStore,
)
from primer.int.storage import Storage
from primer.int.storage_provider import StorageProvider
from primer.model.common import Identifiable, dump_for_storage
from primer.model.system_state import SystemState
from primer.model.except_ import BadRequestError, ConfigError, ConflictError, NotFoundError, ProviderError, ServerError
from primer.model.provider import SqliteConfig
from primer.model.storage import (
    CursorPage,
    CursorPageResponse,
    OffsetPage,
    OffsetPageResponse,
    OrderBy,
    PageRequest,
    Predicate,
)
from primer.storage._cursor import (
    _decode_cursor,
    _encode_cursor_for,
)
from primer.storage._sqlite_predicate import (
    _SqlitePredicateTranslator,
    _render_field_expr,
    _render_typed_field_expr,
    render_order_by_sqlite,
)


logger = logging.getLogger(__name__)


ModelT = TypeVar("ModelT", bound=Identifiable)


def _table_name_for(model_class: type[BaseModel]) -> str:
    """Derive a table name from a model class.

    Mirrors :func:`primer.storage.postgres._table_name_for` exactly so
    the two backends place each model in the same-named table; an
    operator can swap providers without renaming anything.
    """
    name = model_class.__name__.lower()
    if name in ("session", "workspacesession"):
        return "sessions"
    return name


def _escape_like(value: str) -> str:
    """Escape SQL ``LIKE`` metacharacters in a value bound as a literal prefix.

    Escapes the escape char first, then ``%`` and ``_``, so the value matches
    LITERALLY under a ``LIKE ? ESCAPE '\\'`` clause. Used by the content
    store's prefix listing so a path prefix containing ``%`` or ``_`` does not
    over-match.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class SqliteStorageProvider(StorageProvider):
    """Storage provider backed by a single embedded SQLite file."""

    def __init__(self, config: SqliteConfig) -> None:
        self._config = config
        self._conn: aiosqlite.Connection | None = None
        self._handles: dict[type[Identifiable], SqliteStorage[Any]] = {}
        # SQLite uses ONE shared aiosqlite connection for every Storage handle,
        # the claim engine, the scheduler, and every concurrent request. A
        # transaction (BEGIN..COMMIT) on that single connection is therefore a
        # PROCESS-WIDE critical section: while one is open, NO other write may
        # touch the connection, or it would be silently swept into (or lost
        # by) the transaction. ``_write_lock`` serialises every write -- both
        # standalone single-statement writes and the whole transactional unit
        # -- so no foreign write can interleave between BEGIN and COMMIT. The
        # transaction captures only its own statements; unrelated writes wait
        # for the lock and then commit independently.
        self._write_lock = asyncio.Lock()
        # The asyncio Task that currently holds an open ``transaction()``
        # block, or ``None``. Writes issued from THAT task (the document
        # entity + body writes the transaction itself performs) must NOT try
        # to re-acquire ``_write_lock`` (it is already held -> deadlock) and
        # must NOT commit on their own (the transaction commits them as a
        # unit). Any OTHER task's write is a foreign write: it blocks on the
        # lock and gets its own commit.
        self._txn_task: asyncio.Task[Any] | None = None

    def _in_own_transaction(self) -> bool:
        """True iff the calling task is the one that opened an active
        :meth:`transaction` block (so its writes are reentrant: skip the
        lock + skip the per-write commit)."""
        if self._txn_task is None:
            return False
        try:
            return asyncio.current_task() is self._txn_task
        except RuntimeError:  # pragma: no cover - no running loop
            return False

    @asynccontextmanager
    async def _write_guard(self):
        """Serialise a single write on the shared connection.

        Acquires ``_write_lock`` unless the caller is already inside its own
        transaction (which holds the lock for the whole BEGIN..COMMIT). Yields
        ``True`` when the caller should commit its own statement (standalone
        write), ``False`` when it must defer to the enclosing transaction.
        """
        if self._in_own_transaction():
            # Reentrant write from inside the held transaction: the lock is
            # already held by this task and the transaction owns the commit.
            yield False
            return
        async with self._write_lock:
            yield True

    @asynccontextmanager
    async def transaction(self):
        """Group writes on the shared connection into one atomic transaction.

        Holds ``_write_lock`` for the entire ``BEGIN``..``COMMIT``/``ROLLBACK``
        so no other coroutine can write to the shared connection while the
        transaction is open -- the transaction therefore captures only its own
        statements and a concurrent unrelated write keeps its own independent
        durability (it simply waits for the lock). Yields the shared connection
        so callers thread it as the ``conn`` kwarg for backend parity; SQLite
        ignores the value (reentrancy is detected via the owning task), but
        threading it keeps the call sites identical to Postgres.

        Issues ``COMMIT`` on clean exit and ``ROLLBACK`` if the body raises.
        A ``try/finally`` always clears ``_txn_task`` even if COMMIT itself
        raises, so the connection can never be left in a "skip commit" state.
        Not re-entrant: a nested ``transaction()`` from the same task raises
        ``ConfigError`` (and never deadlocks).
        """
        if self._in_own_transaction():
            raise ConfigError(
                "SqliteStorageProvider.transaction() is not re-entrant"
            )
        async with self._write_lock:
            conn = self.connection
            # BEGIN before claiming ownership: if BEGIN itself raises we leave
            # ``_txn_task`` None (the lock releases via the context manager) so
            # the next caller is not wedged into a phantom transaction.
            await conn.execute("BEGIN")
            self._txn_task = asyncio.current_task()
            try:
                yield conn
            except BaseException:
                try:
                    await conn.rollback()
                finally:
                    self._txn_task = None
                raise
            else:
                try:
                    await conn.commit()
                finally:
                    self._txn_task = None

    @asynccontextmanager
    async def read_snapshot(self):
        """Group a set of reads into ONE consistent snapshot.

        Holds ``_write_lock`` (and issues ``BEGIN``..``COMMIT``) across the
        enclosed reads so no foreign write can commit between them. Motivating
        case: a paginated ``list()`` runs its page ``SELECT`` and its
        ``COUNT(*)`` as two statements on the shared connection with an
        ``await`` between them; without this a concurrent ``create``/``delete``
        could slip in and make the reported ``total`` disagree with the page it
        accompanies. Because every write goes through :meth:`_write_guard`
        (which acquires the same lock), holding it here freezes the data set for
        the whole read pair.

        When the caller is already inside its OWN :meth:`transaction`, the reads
        are already serialised under the held lock, so run them inline --
        opening a nested ``BEGIN`` would fail and the outer unit owns the
        commit. This makes the snapshot re-entrant and deadlock-free.
        """
        if self._in_own_transaction():
            yield
            return
        async with self._write_lock:
            conn = self.connection
            await conn.execute("BEGIN")
            try:
                yield
            except BaseException:
                # Read-only unit, but roll back to close the snapshot txn
                # cleanly on error (mirrors :meth:`transaction`).
                await conn.rollback()
                raise
            else:
                # Nothing to persist, but COMMIT ends the snapshot transaction
                # so the connection returns to autocommit and never lingers
                # with an open BEGIN.
                await conn.commit()

    @property
    def in_transaction(self) -> bool:
        """True while any :meth:`transaction` block is open on this provider.

        Retained for diagnostics / backward compatibility. Per-write commit
        decisions now flow through :meth:`_write_guard` (which is task-aware),
        NOT this flag, so a foreign write is never misled into skipping its
        own commit."""
        return self._txn_task is not None

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
            # SQLite's LIKE is case-insensitive for ASCII by default,
            # whereas Postgres LIKE is case-sensitive. The Storage
            # Protocol mandates case-SENSITIVE LIKE; COLLATE has no
            # effect on LIKE in SQLite, so pin it via this connection-
            # scoped pragma (this provider owns its connection, so the
            # global scope affects only primer's own queries).
            await self._conn.execute("PRAGMA case_sensitive_like = ON")
            await self._conn.execute(
                "CREATE TABLE IF NOT EXISTS leases ("
                "  kind              TEXT NOT NULL,"
                "  entity_id         TEXT NOT NULL,"
                "  claimed_by        TEXT,"
                "  claimed_at        TEXT,"
                "  last_heartbeat_at TEXT,"
                "  expires_at        TEXT,"
                "  next_attempt_at   TEXT NOT NULL DEFAULT (datetime('now')),"
                "  priority_score    INTEGER NOT NULL DEFAULT 100,"
                "  attempt_count     INTEGER NOT NULL DEFAULT 0,"
                "  last_error        TEXT,"
                "  PRIMARY KEY (kind, entity_id)"
                ")"
            )
            await self._conn.execute(
                "CREATE INDEX IF NOT EXISTS leases_claim_order "
                "ON leases (priority_score, next_attempt_at) "
                "WHERE claimed_by IS NULL"
            )
            await self._conn.execute(
                "CREATE TABLE IF NOT EXISTS system_state ("
                "  id                     TEXT PRIMARY KEY DEFAULT 'singleton',"
                "  bootstrap_completed_at TEXT,"
                "  schema_version         INTEGER NOT NULL DEFAULT 1,"
                "  last_migration_at      TEXT,"
                "  session_secret         TEXT"
                ")"
            )
            # Schema-evolution shim: for installs created before
            # session_secret existed, add the column if missing. ALTER TABLE
            # ADD COLUMN is idempotent across SQLite versions via a guard.
            cur = await self._conn.execute("PRAGMA table_info(system_state)")
            cols = {row[1] for row in await cur.fetchall()}
            if "session_secret" not in cols:
                await self._conn.execute(
                    "ALTER TABLE system_state ADD COLUMN session_secret TEXT"
                )
            await self._conn.execute(
                "INSERT INTO system_state (id) VALUES ('singleton') "
                "ON CONFLICT DO NOTHING"
            )
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

    async def get_system_state(self) -> SystemState:
        """Return the singleton ``system_state`` row."""
        sql = (
            "SELECT id, bootstrap_completed_at, schema_version, "
            "       last_migration_at, session_secret "
            "FROM system_state WHERE id = ?"
        )
        cur = await self.connection.execute(sql, ("singleton",))
        row = await cur.fetchone()
        if row is None:
            return SystemState()
        id_, bca_text, schema_version, lma_text, session_secret = row

        def _parse_ts(s: str | None) -> datetime | None:
            if s is None:
                return None
            # SQLite stores ISO-8601 UTC strings; ensure timezone-aware.
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt

        return SystemState(
            id=id_,
            bootstrap_completed_at=_parse_ts(bca_text),
            schema_version=schema_version,
            last_migration_at=_parse_ts(lma_text),
            session_secret=session_secret,
        )

    async def set_bootstrap_completed(self, ts: datetime) -> None:
        """Stamp ``bootstrap_completed_at`` on the singleton row."""
        # Store as ISO-8601 UTC string.
        ts_str = ts.isoformat()
        sql = (
            "UPDATE system_state SET bootstrap_completed_at = ? WHERE id = ?"
        )
        await self.connection.execute(sql, (ts_str, "singleton"))
        await self.connection.commit()

    async def set_session_secret(self, secret: str) -> None:
        """Persist the cookie-signing HMAC secret on the singleton row.

        Called by the auth layer the first time it needs a secret and
        ``PRIMER_SESSION_SECRET`` env var is not set.
        """
        await self.connection.execute(
            "UPDATE system_state SET session_secret = ? WHERE id = ?",
            (secret, "singleton"),
        )
        await self.connection.commit()

    def get_content_store(self) -> "SqliteDocumentContentStore":
        """Return the document-body store bound to this provider's connection."""
        return SqliteDocumentContentStore(self)


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
            # Serialise the DDL on the shared write lock and only commit when
            # NOT inside an enclosing transaction. The unconditional commit
            # this used to do would prematurely commit an in-progress
            # transactional unit (a torn write: the first document write runs
            # _ensure_table -> committed the entity early, so a failing body
            # write left a committed orphan). Inside a transaction the DDL is
            # part of the unit and is committed/rolled back with it; since the
            # DDL is ``IF NOT EXISTS`` a rollback simply re-runs it next time.
            async with self._provider._write_guard() as should_commit:  # noqa: SLF001
                await conn.execute(ddl)
                if should_commit:
                    await conn.commit()
        except Exception as exc:
            raise _wrap_sqlite_error(
                exc, model_name=self._model.__name__, op="ensure_table",
            ) from exc
        else:
            # Only cache "ensured" when the DDL was COMMITTED. When it ran
            # deferred inside an enclosing transaction (should_commit False),
            # that transaction may still ROLL BACK and undo the CREATE; caching
            # True would then skip the DDL on the next write and hit a missing
            # table. Re-running the idempotent ``IF NOT EXISTS`` DDL next time
            # is cheap and correct.
            if should_commit:
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

    async def get(self, id: str, *, conn: object | None = None) -> ModelT | None:  # noqa: A002
        # SQLite uses a single shared connection so there is nothing to
        # thread; accept the kwarg for Protocol parity and ignore it.
        del conn
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

    async def create(self, entity: ModelT, *, conn: object | None = None) -> ModelT:
        # SQLite uses a single shared connection so there is nothing to
        # thread; accept the kwarg for Protocol parity and ignore it.
        del conn
        await self._ensure_table()
        entity_id, data_json = self._to_row(entity)
        sql = (
            f'INSERT INTO "{self._table}" (id, data) VALUES (?, ?) '
            f"RETURNING id, data"
        )
        try:
            async with self._provider._write_guard() as should_commit:  # noqa: SLF001
                cur = await self._provider.connection.execute(
                    sql, (entity_id, data_json),
                )
                row = await cur.fetchone()
                if should_commit:
                    await self._provider.connection.commit()
        except Exception as exc:
            raise _wrap_sqlite_error(
                exc, model_name=self._model.__name__, op="create",
            ) from exc
        # RETURNING guarantees a row on success.
        assert row is not None
        return self._from_row(row[0], row[1])

    async def update(self, entity: ModelT, *, conn: object | None = None) -> ModelT:
        # SQLite uses a single shared connection so there is nothing to
        # thread; accept the kwarg for Protocol parity and ignore it.
        del conn
        await self._ensure_table()
        entity_id, data_json = self._to_row(entity)
        sql = (
            f'UPDATE "{self._table}" '
            f"SET data = ?, updated_at = datetime('now') "
            f"WHERE id = ? RETURNING id, data"
        )
        try:
            async with self._provider._write_guard() as should_commit:  # noqa: SLF001
                cur = await self._provider.connection.execute(
                    sql, (data_json, entity_id),
                )
                row = await cur.fetchone()
                if should_commit:
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

    async def delete(self, id: str, *, conn: object | None = None) -> None:  # noqa: A002
        # SQLite uses a single shared connection so there is nothing to
        # thread; accept the kwarg for Protocol parity and ignore it.
        del conn
        await self._ensure_table()
        sql = f'DELETE FROM "{self._table}" WHERE id = ?'
        try:
            async with self._provider._write_guard() as should_commit:  # noqa: SLF001
                cur = await self._provider.connection.execute(sql, (id,))
                if should_commit:
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
            # Run the page SELECT and its COUNT(*) inside ONE consistent read
            # snapshot so a concurrent write can't slip between the two
            # statements and make ``total`` disagree with the returned page
            # (BE10a). ``read_snapshot`` holds the shared write lock for both.
            async with self._provider.read_snapshot():
                cur = await self._provider.connection.execute(select_sql, params)
                rows = await cur.fetchall()
                cur = await self._provider.connection.execute(
                    count_sql, count_params,
                )
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
            {"keys": [{"field", "value", "direction", "is_null"}, ...,
                      {"field": "id", ...}]}

        Null-safe lexicographic-expansion seek (same shape as the
        Postgres backend; only the cell expressions differ). Each key's
        ordering tuple is ``((field IS NULL) ASC, field <dir>)`` so the
        seek must compare the NULL flag ahead of the value, otherwise a
        ``field > NULL`` comparison is UNKNOWN and silently drops every
        row at/after the first NULL.
        """
        keys = cursor_state.get("keys", [])
        if not keys:
            raise BadRequestError("cursor missing 'keys'")
        clauses: list[str] = []
        for prefix_len in range(len(keys)):
            parts: list[str] = []
            for k in keys[:prefix_len]:
                parts.append(self._cursor_key_eq(translator, k))
            parts.append(self._cursor_key_gt(translator, keys[prefix_len]))
            clauses.append("(" + " AND ".join(parts) + ")")
        return "(" + " OR ".join(clauses) + ")"

    def _cursor_key_exprs(self, k: dict[str, Any]) -> tuple[str, str]:
        """Return ``(null_flag_expr, value_expr)`` for a cursor key."""
        if k["field"] == "id":
            return "0", "id"
        raw = _render_field_expr(self._model, k["field"])
        return f"({raw} IS NULL)", _render_typed_field_expr(self._model, k["field"])

    def _cursor_key_eq(
        self, translator: _SqlitePredicateTranslator, k: dict[str, Any]
    ) -> str:
        """Equality of a cursor key, NULL-safe (both-null counts as equal)."""
        null_expr, val_expr = self._cursor_key_exprs(k)
        if k.get("is_null"):
            return f"({null_expr} = 1)"
        ph = translator.append_param(k["value"])
        return f"(({null_expr} = 0) AND ({val_expr} = {ph}))"

    def _cursor_key_gt(
        self, translator: _SqlitePredicateTranslator, k: dict[str, Any]
    ) -> str:
        """Strict "past this key" in the key's own direction, NULL-safe.

        Ordering tuple is ``(null_flag ASC, value <dir>)`` so:
          * a non-null cursor key is passed by a NULL row (flag 1 > 0)
            OR a same-flag row whose value is strictly past it;
          * a null cursor key (flag 1, sorts last) can only be passed on
            this key by the id tiebreaker, never by the value, so it
            contributes no value comparison here.
        """
        null_expr, val_expr = self._cursor_key_exprs(k)
        if k.get("is_null"):
            # Nothing sorts after a NULL on this key (NULLs are last);
            # the seek continues only via the id tiebreaker prefix.
            return "(0)"
        sql_op = ">" if k["direction"] == "asc" else "<"
        ph = translator.append_param(k["value"])
        return (
            f"(({null_expr} = 1) OR "
            f"(({null_expr} = 0) AND ({val_expr} {sql_op} {ph})))"
        )


# Map sqlite3 exception classes onto primer domain exceptions.
# Used by CRUD methods in Task 5.
def _wrap_sqlite_error(exc: Exception, *, model_name: str, op: str) -> Exception:
    """Translate sqlite3 errors into primer domain exceptions."""

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


class SqliteDocumentContentStore(DocumentContentStore):
    """SQLite-backed document body store keyed by stable document id.

    Holds bodies in a ``document_content`` table with a
    ``UNIQUE(collection_id, path)`` index, making it authoritative for
    path<->id resolution and path uniqueness. Shares the provider's
    single aiosqlite connection.

    SQLite uses one shared connection, so the ``conn`` kwarg has nothing
    to thread; it is accepted for Protocol parity and ignored. Each write
    commits on its own EXCEPT while the owning provider has a
    :meth:`SqliteStorageProvider.transaction` block open
    (``provider.in_transaction``), in which case the per-write ``commit()``
    is suppressed and the enclosing transaction commits both the entity row
    and the body row together. The conformance suite calls these methods
    outside any transaction, so the per-call commit still happens there.
    """

    def __init__(self, provider: "SqliteStorageProvider") -> None:
        self._provider = provider

    @property
    def _conn(self) -> aiosqlite.Connection:
        return self._provider.connection

    async def ensure_schema(self) -> None:
        try:
            # Serialise on the shared write lock; commit only when not inside
            # an enclosing transaction (the unconditional commit this used to
            # do would prematurely commit an in-progress transactional unit).
            # The DDL is ``IF NOT EXISTS`` so a rollback inside a transaction
            # just re-runs it next time.
            async with self._provider._write_guard() as should_commit:  # noqa: SLF001
                await self._conn.execute(
                    "CREATE TABLE IF NOT EXISTS document_content ("
                    "  document_id   TEXT PRIMARY KEY,"
                    "  collection_id TEXT NOT NULL,"
                    "  path          TEXT NOT NULL,"
                    "  content       TEXT NOT NULL,"
                    "  updated_at    TEXT NOT NULL"
                    ")"
                )
                await self._conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS document_content_coll_path "
                    "ON document_content (collection_id, path)"
                )
                await self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS document_content_coll "
                    "ON document_content (collection_id)"
                )
                if should_commit:
                    await self._conn.commit()
        except Exception as exc:
            raise _wrap_sqlite_error(
                exc, model_name="document_content", op="ensure_schema",
            ) from exc

    async def get(self, document_id: str, *, conn: Any | None = None) -> str | None:
        del conn
        cur = await self._conn.execute(
            "SELECT content FROM document_content WHERE document_id = ?",
            (document_id,),
        )
        row = await cur.fetchone()
        return None if row is None else row[0]

    async def get_by_path(
        self, collection_id: str, path: str, *, conn: Any | None = None
    ) -> ContentRow | None:
        del conn
        cur = await self._conn.execute(
            "SELECT document_id, collection_id, path, content "
            "FROM document_content WHERE collection_id = ? AND path = ?",
            (collection_id, path),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return ContentRow(
            document_id=row[0],
            collection_id=row[1],
            path=row[2],
            content=row[3],
        )

    async def resolve_id(
        self, collection_id: str, path: str, *, conn: Any | None = None
    ) -> str | None:
        del conn
        cur = await self._conn.execute(
            "SELECT document_id FROM document_content "
            "WHERE collection_id = ? AND path = ?",
            (collection_id, path),
        )
        row = await cur.fetchone()
        return None if row is None else row[0]

    async def upsert(
        self,
        *,
        document_id: str,
        collection_id: str,
        path: str,
        content: str,
        conn: Any | None = None,
    ) -> None:
        del conn
        now = datetime.now(timezone.utc).isoformat()
        try:
            async with self._provider._write_guard() as should_commit:  # noqa: SLF001
                await self._conn.execute(
                    "INSERT INTO document_content"
                    " (document_id, collection_id, path, content, updated_at)"
                    " VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT(document_id) DO UPDATE SET"
                    "   collection_id = excluded.collection_id,"
                    "   path = excluded.path,"
                    "   content = excluded.content,"
                    "   updated_at = excluded.updated_at",
                    (document_id, collection_id, path, content, now),
                )
                if should_commit:
                    await self._conn.commit()
        except Exception as exc:
            raise _wrap_sqlite_error(
                exc, model_name="document_content", op="upsert",
            ) from exc

    async def delete(self, document_id: str, *, conn: Any | None = None) -> None:
        del conn
        try:
            async with self._provider._write_guard() as should_commit:  # noqa: SLF001
                await self._conn.execute(
                    "DELETE FROM document_content WHERE document_id = ?",
                    (document_id,),
                )
                if should_commit:
                    await self._conn.commit()
        except Exception as exc:
            raise _wrap_sqlite_error(
                exc, model_name="document_content", op="delete",
            ) from exc

    async def move(
        self, document_id: str, new_path: str, *, conn: Any | None = None
    ) -> None:
        del conn
        now = datetime.now(timezone.utc).isoformat()
        try:
            async with self._provider._write_guard() as should_commit:  # noqa: SLF001
                cur = await self._conn.execute(
                    "UPDATE document_content SET path = ?, updated_at = ? "
                    "WHERE document_id = ?",
                    (new_path, now, document_id),
                )
                if should_commit:
                    await self._conn.commit()
                rowcount = cur.rowcount
        except Exception as exc:
            raise _wrap_sqlite_error(
                exc, model_name="document_content", op="move",
            ) from exc
        if rowcount == 0:
            raise NotFoundError(
                f"document_content with document_id {document_id!r} not found"
            )

    async def list(
        self, collection_id: str, *, prefix: str | None = None
    ) -> list[ContentListEntry]:
        sql = (
            "SELECT document_id, path, length(content) AS size "
            "FROM document_content WHERE collection_id = ?"
        )
        params: list[Any] = [collection_id]
        if prefix is not None:
            # Escape LIKE metacharacters in the bound prefix so a `%`/`_`
            # in the path prefix matches LITERALLY rather than as a wildcard
            # (would otherwise over-match). The escape char is backslash.
            sql += " AND path LIKE ? || '%' ESCAPE '\\'"
            params.append(_escape_like(prefix))
        try:
            cur = await self._conn.execute(sql, params)
            rows = await cur.fetchall()
        except Exception as exc:
            raise _wrap_sqlite_error(
                exc, model_name="document_content", op="list",
            ) from exc
        return [
            ContentListEntry(document_id=r[0], path=r[1], size=int(r[2]))
            for r in rows
        ]


__all__ = [
    "SqliteDocumentContentStore",
    "SqliteStorage",
    "SqliteStorageProvider",
]
