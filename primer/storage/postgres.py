"""Postgres-backed :class:`StorageProvider` and :class:`Storage[T]`.

Generic-over-model implementation backed by JSONB tables. Every model
class registered via :meth:`PostgresStorageProvider.get_storage` lives
in its own table:

.. code-block:: sql

    CREATE TABLE IF NOT EXISTS <schema>.<table> (
        id          text PRIMARY KEY,
        data        jsonb NOT NULL,
        created_at  timestamptz NOT NULL DEFAULT now(),
        updated_at  timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS <table>_data_gin
        ON <schema>.<table> USING gin (data jsonb_path_ops);

The ``id`` column is hoisted out of the JSON document so it can be
the table's primary key with the obvious B-tree index. Everything
else lives under ``data`` (Pydantic's ``model_dump(mode="json")``
output minus ``id``). The GIN index makes containment-style
predicate scans fast; ``WHERE`` fragments produced by
:mod:`primer.storage._predicate` use ``data->>'field'`` paths that
remain index-friendly when combined with that GIN.

Pagination supports both styles required by :class:`primer.int.Storage`:

* :class:`primer.model.storage.OffsetPage` -> SQL ``LIMIT/OFFSET`` plus
  a separate ``COUNT(*)`` for the response's ``total``.
* :class:`primer.model.storage.CursorPage` -> keyset seek using the
  ``ORDER BY`` keys + the primary key as the tiebreaker. Cursors are
  opaque base64 JSON; their structure is internal to this module.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, TypeVar

import asyncpg

from primer.storage._ddl import (
    CONCURRENT_CREATE_RACE,
    execute_create_idempotent,
)
from pydantic import BaseModel

from primer.int.storage import Storage
from primer.int.storage_provider import StorageProvider
from primer.model.common import Identifiable, dump_for_storage
from primer.model.system_state import SystemState
from primer.model.except_ import (
    BadRequestError,
    ConfigError,
    ConflictError,
    NotFoundError,
    ProviderError,
    ServerError,
)
from primer.model.provider import PostgresConfig
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
    _resolve_dotted,
)
from primer.storage._predicate import _PredicateTranslator, render_order_by


logger = logging.getLogger(__name__)


ModelT = TypeVar("ModelT", bound=Identifiable)


# Identifier whitelist. Postgres identifiers can be much richer when
# double-quoted, but we restrict to the safe ASCII subset to keep
# table names predictable, comparable across deployments, and free
# of any quoting surprises.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _sanitize_identifier(name: str, *, kind: str) -> str:
    """Validate that ``name`` is a safe SQL identifier.

    Raises :class:`ConfigError` if not. Used for schema names. (Table
    names are derived from class names, which are valid identifiers
    by construction in any well-formed Python program.)
    """
    if not _IDENT_RE.match(name):
        raise ConfigError(
            f"{kind} {name!r} must match {_IDENT_RE.pattern} (safe SQL identifier subset)"
        )
    return name


def _table_name_for(model_class: type[BaseModel]) -> str:
    """Derive a table name from a model class.

    Convention: class name lowercased. ``Document`` -> ``"document"``,
    ``LLMProvider`` -> ``"llmprovider"``. Applications that want
    a different mapping (pluralisation, namespacing) can wrap
    ``get_storage`` in their own factory; this convention is
    deterministic and obvious.

    One historic exception: the ``Session`` model is stored in a
    ``sessions`` (plural) table. The scheduler layer
    (``primer.scheduler.postgres``) hard-codes ``sessions`` in its
    JOINs and FK constraints, and the scheduler unit tests assume the
    same name. Mapping ``Session`` -> ``sessions`` here keeps both
    sides in sync without rewriting every SQL string in the scheduler.
    """
    name = model_class.__name__.lower()
    if name in ("session", "workspacesession"):
        return "sessions"
    return name


# ===========================================================================
# Provider
# ===========================================================================


class PostgresStorageProvider(StorageProvider):
    """Storage provider backed by a single Postgres database.

    Owns the asyncpg connection pool. Multiple :class:`PostgresStorage`
    handles created via :meth:`get_storage` share the same pool. The
    provider does NOT eagerly create tables -- table DDL runs on
    first use of each model's handle so unused models don't pollute
    the schema.
    """

    def __init__(self, config: PostgresConfig) -> None:
        self._config = config
        self._schema = _sanitize_identifier(config.db_schema, kind="schema")
        self._pool: asyncpg.Pool | None = None
        self._handles: dict[type[Identifiable], PostgresStorage[Any]] = {}

    @property
    def pool(self) -> asyncpg.Pool:
        """The shared asyncpg pool. Raises if not :meth:`initialize`-d."""
        if self._pool is None:
            raise ConfigError(
                "PostgresStorageProvider used before initialize()"
            )
        return self._pool

    @property
    def schema(self) -> str:
        """Sanitised schema name where tables are created."""
        return self._schema

    @property
    def rate_limit_lease_table(self) -> str:
        """Schema-qualified table name for coordinator rate-limiter leases."""
        return f'"{self._schema}"."rate_limit_lease"'

    @property
    def leader_lease_table(self) -> str:
        """Schema-qualified table name for coordinator leadership leases."""
        return f'"{self._schema}"."leader_lease"'

    @property
    def leases_table(self) -> str:
        """Schema-qualified table name for claim-engine leases."""
        return f'"{self._schema}"."leases"'

    @property
    def system_state_table(self) -> str:
        """Schema-qualified table name for the system-state singleton."""
        return f'"{self._schema}"."system_state"'

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
            )
        except Exception as exc:
            raise ProviderError(
                f"failed to open Postgres connection pool: {exc}",
                cause=exc,
            ) from exc

        # Make sure the target schema exists. This is the only DDL the
        # provider runs eagerly; per-model tables are deferred to first
        # use of the handle.
        # All CREATE statements below run as autocommit (no enclosing
        # transaction) and tolerate the cold-start race -- when several
        # processes boot against a fresh schema at once they contend on the
        # system catalogs even with IF NOT EXISTS (see primer.storage._ddl).
        # Each statement is attempted independently so a race on one does
        # not skip the rest.
        async with self._pool.acquire() as conn:
            await execute_create_idempotent(
                conn, f'CREATE SCHEMA IF NOT EXISTS "{self._schema}"'
            )
            # Coordinator lease tables live in the same schema as the
            # rest of primer so two deployments sharing a Postgres
            # cluster with distinct db_schema settings keep their
            # leases isolated. Coordinator backends read these names
            # off the provider via .rate_limit_lease_table / .leader_lease_table.
            await execute_create_idempotent(
                conn,
                f'CREATE TABLE IF NOT EXISTS "{self._schema}"."rate_limit_lease" ('
                f'  lease_id   TEXT PRIMARY KEY,'
                f'  key        TEXT NOT NULL,'
                f'  owner_id   TEXT NOT NULL,'
                f'  claimed_at TIMESTAMPTZ NOT NULL,'
                f'  expires_at TIMESTAMPTZ NOT NULL'
                f')',
            )
            await execute_create_idempotent(
                conn,
                f'CREATE INDEX IF NOT EXISTS rate_limit_lease_key_active '
                f'ON "{self._schema}"."rate_limit_lease" (key, expires_at)',
            )
            await execute_create_idempotent(
                conn,
                f'CREATE TABLE IF NOT EXISTS "{self._schema}"."leader_lease" ('
                f'  role       TEXT PRIMARY KEY,'
                f'  owner_id   TEXT NOT NULL,'
                f'  claimed_at TIMESTAMPTZ NOT NULL,'
                f'  expires_at TIMESTAMPTZ NOT NULL'
                f')',
            )
            await execute_create_idempotent(
                conn,
                f'CREATE TABLE IF NOT EXISTS "{self._schema}"."leases" ('
                f'  kind              TEXT NOT NULL,'
                f'  entity_id         TEXT NOT NULL,'
                f'  claimed_by        TEXT,'
                f'  claimed_at        TIMESTAMPTZ,'
                f'  last_heartbeat_at TIMESTAMPTZ,'
                f'  expires_at        TIMESTAMPTZ,'
                f'  next_attempt_at   TIMESTAMPTZ NOT NULL DEFAULT now(),'
                f'  priority_score    INTEGER NOT NULL DEFAULT 100,'
                f'  attempt_count     INTEGER NOT NULL DEFAULT 0,'
                f'  last_error        TEXT,'
                f'  PRIMARY KEY (kind, entity_id)'
                f')',
            )
            await execute_create_idempotent(
                conn,
                f'CREATE INDEX IF NOT EXISTS leases_claim_order '
                f'ON "{self._schema}"."leases" (priority_score, next_attempt_at) '
                f'WHERE claimed_by IS NULL',
            )
            await execute_create_idempotent(
                conn,
                f'CREATE TABLE IF NOT EXISTS "{self._schema}"."system_state" ('
                f'  id                     TEXT PRIMARY KEY DEFAULT \'singleton\','
                f'  bootstrap_completed_at TIMESTAMPTZ,'
                f'  schema_version         INTEGER NOT NULL DEFAULT 1,'
                f'  last_migration_at      TIMESTAMPTZ,'
                f'  session_secret         TEXT'
                f')',
            )
            # Schema-evolution: add session_secret column on pre-existing
            # tables. Idempotent: only adds when missing.
            await conn.execute(
                f'ALTER TABLE "{self._schema}"."system_state" '
                f'ADD COLUMN IF NOT EXISTS session_secret TEXT'
            )
            await conn.execute(
                f'INSERT INTO "{self._schema}"."system_state" (id) '
                f"VALUES ('singleton') ON CONFLICT DO NOTHING"
            )
        logger.info(
            "PostgresStorageProvider initialised (schema=%r, host=%s:%d)",
            self._schema,
            cfg.hostname,
            cfg.port,
        )

    async def aclose(self) -> None:
        if self._pool is None:
            return
        await self._pool.close()
        self._pool = None
        self._handles.clear()
        logger.info("PostgresStorageProvider closed")

    def get_storage(self, model_class: type[ModelT]) -> Storage[ModelT]:
        cached = self._handles.get(model_class)
        if cached is not None:
            return cached
        handle = PostgresStorage[ModelT](
            provider=self,
            model_class=model_class,
        )
        self._handles[model_class] = handle
        return handle

    async def get_system_state(self) -> SystemState:
        """Return the singleton ``system_state`` row."""
        sql = (
            f'SELECT id, bootstrap_completed_at, schema_version, '
            f'       last_migration_at, session_secret '
            f'FROM {self.system_state_table} WHERE id = $1'
        )
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(sql, "singleton")
        if row is None:
            # Shouldn't happen after initialize(), but return a safe default.
            return SystemState()
        return SystemState(
            id=row["id"],
            bootstrap_completed_at=row["bootstrap_completed_at"],
            schema_version=row["schema_version"],
            last_migration_at=row["last_migration_at"],
            session_secret=row["session_secret"],
        )

    async def set_bootstrap_completed(self, ts: datetime) -> None:
        """Stamp ``bootstrap_completed_at`` on the singleton row."""
        sql = (
            f'UPDATE {self.system_state_table} '
            f'SET bootstrap_completed_at = $1 WHERE id = $2'
        )
        async with self.pool.acquire() as conn:
            await conn.execute(sql, ts, "singleton")

    async def set_session_secret(self, secret: str) -> None:
        """Persist the cookie-signing HMAC secret on the singleton row."""
        sql = (
            f'UPDATE {self.system_state_table} '
            f'SET session_secret = $1 WHERE id = $2'
        )
        async with self.pool.acquire() as conn:
            await conn.execute(sql, secret, "singleton")


# ===========================================================================
# Per-model handle
# ===========================================================================


# Module-level cache of "we've ensured the table exists for this class
# on this provider". Keyed by (provider id, model class). Avoids a
# round-trip to Postgres on every operation while staying correct when
# the same provider is shared across handles.
_table_ensured: set[tuple[int, type[BaseModel]]] = set()


class PostgresStorage(Storage[ModelT]):
    """Per-model :class:`Storage` handle backed by a JSONB table."""

    def __init__(
        self,
        provider: PostgresStorageProvider,
        model_class: type[ModelT],
    ) -> None:
        self._provider = provider
        self._model = model_class
        self._table = _table_name_for(model_class)
        # Quoted, schema-qualified table reference for inline SQL.
        self._qualified = f'"{provider.schema}"."{self._table}"'

    # ---------- DDL --------------------------------------------------------

    async def _ensure_table(self) -> None:
        cache_key = (id(self._provider), self._model)
        if cache_key in _table_ensured:
            return
        ddl_table = (
            f'CREATE TABLE IF NOT EXISTS {self._qualified} ('
            'id text PRIMARY KEY, '
            'data jsonb NOT NULL, '
            'created_at timestamptz NOT NULL DEFAULT now(), '
            'updated_at timestamptz NOT NULL DEFAULT now()'
            ')'
        )
        ddl_index = (
            f'CREATE INDEX IF NOT EXISTS '
            f'"{self._table}_data_gin" '
            f'ON {self._qualified} USING gin (data jsonb_path_ops)'
        )
        try:
            async with self._provider.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(ddl_table)
                    await conn.execute(ddl_index)
        except CONCURRENT_CREATE_RACE as exc:
            # Concurrent-creation race (see primer.storage._ddl): another
            # process won the race and created the table + index atomically
            # in one transaction, so the object exists now -- treat as
            # ensured rather than crashing startup.
            logger.debug(
                "ensure_table race on %s (%s); treating as already created",
                self._qualified, type(exc).__name__,
            )
        except Exception as exc:
            raise ProviderError(
                f"failed to create table {self._qualified}: {exc}",
                cause=exc,
            ) from exc
        _table_ensured.add(cache_key)

    # ---------- Serialisation ---------------------------------------------

    def _to_row(self, entity: ModelT) -> tuple[str, str]:
        # Dump the full model, then strip ``id`` from the JSONB payload
        # (the column carries it). ``dump_for_storage`` performs a
        # ``mode="json"`` dump and unmasks SecretStr fields back to
        # their plaintext values so credentials round-trip correctly --
        # the default ``model_dump(mode="json")`` would replace every
        # SecretStr with ``"**********"``.
        dumped = dump_for_storage(entity)
        entity_id = dumped.pop("id")
        return entity_id, json.dumps(dumped)

    def _from_row(self, row: asyncpg.Record) -> ModelT:
        # Reconstruct the model from id + data JSONB. asyncpg returns
        # JSONB columns as text by default (we don't register a
        # codec); parse here.
        data = row["data"]
        if isinstance(data, str):
            data = json.loads(data)
        data["id"] = row["id"]
        return self._model.model_validate(data)

    # ---------- get / create / update / delete ----------------------------

    @asynccontextmanager
    async def _acquire_or_use(self, conn: Any | None):
        """Yield a usable connection.

        When ``conn`` is supplied, yield it as-is so the caller's open
        transaction is reused. Otherwise acquire one from the pool for
        the duration of the block.
        """
        if conn is not None:
            yield conn
        else:
            async with self._provider.pool.acquire() as acquired:
                yield acquired

    async def get(self, id: str, *, conn: Any | None = None) -> ModelT | None:
        await self._ensure_table()
        sql = f'SELECT id, data FROM {self._qualified} WHERE id = $1'
        try:
            async with self._acquire_or_use(conn) as c:
                row = await c.fetchrow(sql, id)
        except Exception as exc:
            raise self._wrap_db_error(exc) from exc
        if row is None:
            return None
        return self._from_row(row)

    async def create(self, entity: ModelT) -> ModelT:
        await self._ensure_table()
        entity_id, data_json = self._to_row(entity)
        sql = (
            f'INSERT INTO {self._qualified} (id, data) '
            f'VALUES ($1, $2::jsonb) '
            f'RETURNING id, data'
        )
        try:
            async with self._provider.pool.acquire() as conn:
                row = await conn.fetchrow(sql, entity_id, data_json)
        except asyncpg.UniqueViolationError as exc:
            raise ConflictError(
                f"{self._model.__name__} with id {entity_id!r} already exists",
                cause=exc,
            ) from exc
        except Exception as exc:
            raise self._wrap_db_error(exc) from exc
        return self._from_row(row)

    async def update(self, entity: ModelT, *, conn: Any | None = None) -> ModelT:
        await self._ensure_table()
        entity_id, data_json = self._to_row(entity)
        sql = (
            f'UPDATE {self._qualified} '
            f'SET data = $2::jsonb, updated_at = now() '
            f'WHERE id = $1 '
            f'RETURNING id, data'
        )
        try:
            async with self._acquire_or_use(conn) as c:
                row = await c.fetchrow(sql, entity_id, data_json)
        except Exception as exc:
            raise self._wrap_db_error(exc) from exc
        if row is None:
            raise NotFoundError(
                f"{self._model.__name__} with id {entity_id!r} not found"
            )
        return self._from_row(row)

    async def delete(self, id: str) -> None:
        await self._ensure_table()
        sql = f'DELETE FROM {self._qualified} WHERE id = $1'
        try:
            async with self._provider.pool.acquire() as conn:
                result = await conn.execute(sql, id)
        except Exception as exc:
            raise self._wrap_db_error(exc) from exc
        # asyncpg returns the command tag string e.g. "DELETE 1" / "DELETE 0".
        if result.endswith(" 0"):
            raise NotFoundError(
                f"{self._model.__name__} with id {id!r} not found"
            )

    # ---------- list / find ------------------------------------------------

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

        translator = _PredicateTranslator(self._model)
        where_sql = "TRUE"
        if predicate is not None:
            where_sql, _ = translator.translate(predicate)

        order_clause = render_order_by(self._model, order_by)

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
        raise BadRequestError(f"unknown PageRequest variant {type(page).__name__!r}")

    # ---------- offset pagination -----------------------------------------

    async def _page_offset(
        self,
        *,
        translator: _PredicateTranslator,
        where_sql: str,
        order_clause: str,
        page: OffsetPage,
    ) -> OffsetPageResponse[ModelT]:
        # Bind LIMIT and OFFSET as parameters (cheap, planner-friendly).
        limit_ph = translator.append_param(page.length)
        offset_ph = translator.append_param(page.offset)
        params = translator._params  # noqa: SLF001 -- intentional sibling-module access

        select_sql = (
            f'SELECT id, data FROM {self._qualified} '
            f'WHERE {where_sql} {order_clause} '
            f'LIMIT {limit_ph} OFFSET {offset_ph}'
        )
        # The COUNT(*) re-uses the WHERE clause's params (everything
        # except LIMIT/OFFSET). Slice the first N params to match.
        count_params = params[: -2]
        count_sql = (
            f'SELECT count(*) FROM {self._qualified} WHERE {where_sql}'
        )

        try:
            async with self._provider.pool.acquire() as conn:
                rows = await conn.fetch(select_sql, *params)
                total = await conn.fetchval(count_sql, *count_params)
        except Exception as exc:
            raise self._wrap_db_error(exc) from exc

        items = [self._from_row(r) for r in rows]
        return OffsetPageResponse[self._model](  # type: ignore[name-defined]
            offset=page.offset,
            length=len(items),
            total=int(total) if total is not None else None,
            items=items,
        )

    # ---------- cursor pagination -----------------------------------------

    async def _page_cursor(
        self,
        *,
        translator: _PredicateTranslator,
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

        # Fetch length+1 to detect whether more pages exist.
        limit_ph = translator.append_param(page.length + 1)
        params = translator._params  # noqa: SLF001

        select_sql = (
            f'SELECT id, data FROM {self._qualified} '
            f'WHERE {full_where} {order_clause} '
            f'LIMIT {limit_ph}'
        )

        try:
            async with self._provider.pool.acquire() as conn:
                rows = await conn.fetch(select_sql, *params)
        except Exception as exc:
            raise self._wrap_db_error(exc) from exc

        has_more = len(rows) > page.length
        if has_more:
            rows = rows[: page.length]
        items = [self._from_row(r) for r in rows]

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
        translator: _PredicateTranslator,
        cursor_state: dict[str, Any],
    ) -> str:
        """Render the ``WHERE`` fragment that seeks past the cursor.

        Cursor state shape: ``{"keys": [{"field": str, "value": Any,
        "direction": "asc"|"desc"}, ..., {"field": "id", "value": str,
        "direction": "asc"}]}`` -- the implicit ``id ASC`` tiebreaker
        is always present.

        The seek condition for ``ORDER BY a ASC, b DESC, id ASC`` past
        ``(av, bv, idv)`` is the standard lexicographic expansion::

            (a > av) OR
            (a = av AND b < bv) OR
            (a = av AND b = bv AND id > idv)
        """
        keys = cursor_state.get("keys", [])
        if not keys:
            raise BadRequestError("cursor missing 'keys'")

        clauses: list[str] = []
        for prefix_len in range(len(keys)):
            parts: list[str] = []
            # Equality on the first prefix_len keys (NULL-safe).
            for k in keys[:prefix_len]:
                parts.append(self._cursor_key_eq(translator, k))
            # Strict "past this key" on the prefix_len-th key (NULL-safe).
            parts.append(self._cursor_key_gt(translator, keys[prefix_len]))
            clauses.append("(" + " AND ".join(parts) + ")")
        return "(" + " OR ".join(clauses) + ")"

    def _cursor_key_exprs(self, k: dict[str, Any]) -> tuple[str, str]:
        """Return ``(null_flag_expr, typed_value_expr)`` for a cursor key.

        The null-flag expression yields ``true``/``false``; the value
        expression is the typed field expression (so non-text seek keys
        cast the left side to match the bound Python value -- asyncpg
        rejects a bool/int bind against a bare text ``data->>'k'``).
        """
        from primer.storage._predicate import (  # local import to avoid cycle
            _render_field_expr,
            _render_typed_field_expr,
        )

        if k["field"] == "id":
            return "false", "id"
        raw = _render_field_expr(self._model, k["field"])
        return (
            f"({raw} IS NULL)",
            _render_typed_field_expr(self._model, k["field"]),
        )

    def _cursor_key_eq(
        self, translator: _PredicateTranslator, k: dict[str, Any]
    ) -> str:
        """Equality of a cursor key, NULL-safe (both-null counts as equal)."""
        null_expr, val_expr = self._cursor_key_exprs(k)
        if k.get("is_null"):
            return f"({null_expr} = true)"
        ph = translator.append_param(k["value"])
        return f"(({null_expr} = false) AND ({val_expr} = {ph}))"

    def _cursor_key_gt(
        self, translator: _PredicateTranslator, k: dict[str, Any]
    ) -> str:
        """Strict "past this key" in the key's own direction, NULL-safe.

        Ordering tuple is ``(field IS NULL ASC, value <dir>)`` (NULLS
        LAST), so a non-null cursor key is passed by a NULL row OR a
        same-flag row strictly past it; a null cursor key sorts last and
        can only be passed via the id tiebreaker, contributing no value
        comparison here.
        """
        null_expr, val_expr = self._cursor_key_exprs(k)
        if k.get("is_null"):
            return "(false)"
        sql_op = ">" if k["direction"] == "asc" else "<"
        ph = translator.append_param(k["value"])
        return (
            f"(({null_expr} = true) OR "
            f"(({null_expr} = false) AND ({val_expr} {sql_op} {ph})))"
        )

    # ---------- error mapping ---------------------------------------------

    def _wrap_db_error(self, exc: Exception) -> Exception:
        """Map asyncpg exceptions onto the primer exception hierarchy."""
        if isinstance(exc, asyncpg.UniqueViolationError):
            return ConflictError(str(exc), cause=exc)
        if isinstance(exc, asyncpg.PostgresError):
            return ServerError(f"Postgres error: {exc}", cause=exc)
        return ProviderError(f"storage backend error: {exc}", cause=exc)

