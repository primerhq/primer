"""Persistent routing store: (channel_id, anchor) -> ChannelCorrelation.

Replaces in-memory adapter correlation with a durable SQLite/Postgres-backed
store so routing survives process restarts and works across multi-process
deployments.

Concurrency guarantee
----------------------
``(channel_id, anchor)`` is unique: a DB-level unique index over the JSONB
``data->>'channel_id'`` / ``data->>'anchor'`` expressions is created lazily on
the ``channelcorrelation`` table, and the ``upsert_*`` writes use an atomic
``INSERT ... ON CONFLICT (...) DO UPDATE`` (Postgres) / ``INSERT ... ON
CONFLICT (...) DO UPDATE`` against the SQLite expression index. This makes the
read-modify-write race -- where two workers both observe "no row" and each
insert their own, yielding two correlations for one gate and a double resume --
impossible: the second writer's INSERT collapses onto the first row.

Storage backends without a raw connection (e.g. an in-memory test double)
fall back to the original lookup-then-create/update path, which is still
single-row under the GIL within one process.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from primer.model.channel_correlation import ChannelCorrelation
from primer.model.common import dump_for_storage
from primer.model.storage import OffsetPage
from primer.storage.q import Q


logger = logging.getLogger(__name__)



# Name of the JSONB table the ChannelCorrelation model is stored in
# (model class name lowercased -- see primer.storage.{postgres,sqlite}
# ._table_name_for). Hoisted here so the unique-index DDL targets the
# same table the generic Storage[T] handle writes to.
_TABLE = "channelcorrelation"
_UNIQUE_INDEX = "channelcorrelation_channel_anchor_uniq"


class CorrelationStore:
    """CRUD wrapper around :class:`~primer.model.channel_correlation.ChannelCorrelation`.

    Parameters
    ----------
    storage_provider:
        A :class:`~primer.int.storage_provider.StorageProvider` instance
        (SQLite or Postgres) that has already been initialized.
    """

    def __init__(self, storage_provider: object) -> None:
        self._sp = storage_provider
        # One-shot guard so the unique-index DDL runs at most once per store.
        self._unique_index_ensured = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _storage(self):
        return self._sp.get_storage(ChannelCorrelation)

    def _backend(self) -> str:
        """Classify the storage provider: ``"postgres"``, ``"sqlite"``, or
        ``"other"`` (no raw-connection upsert -- use the fallback path)."""
        cls = type(self._sp).__name__
        if cls == "PostgresStorageProvider":
            return "postgres"
        if cls == "SqliteStorageProvider":
            return "sqlite"
        return "other"

    async def lookup(self, channel_id: str, anchor: str) -> ChannelCorrelation | None:
        """Return the correlation record for *(channel_id, anchor)*, or ``None``."""
        page = await self._storage().find(
            Q(ChannelCorrelation)
            .where("channel_id", channel_id)
            .where("anchor", anchor)
            .build(),
            OffsetPage(offset=0, length=2),
        )
        return page.items[0] if page.items else None

    # ------------------------------------------------------------------
    # Atomic upsert plumbing
    # ------------------------------------------------------------------

    async def _ensure_unique_index(self) -> None:
        """Create the ``(channel_id, anchor)`` unique index once.

        Targets the JSONB ``data`` column's extracted scalars because the
        generic :class:`Storage` lays every model out as ``(id, data)`` with
        the model's own fields living inside ``data`` (see the storage docs).
        Idempotent (``IF NOT EXISTS``). Ensures the underlying table exists
        first by touching the handle.
        """
        if self._unique_index_ensured:
            return
        backend = self._backend()
        # Touch the handle so the base table DDL has run before we index it.
        storage = self._storage()
        await storage.get("__index_bootstrap__")
        if backend == "postgres":
            schema = self._sp.schema
            sql = (
                f'CREATE UNIQUE INDEX IF NOT EXISTS "{_UNIQUE_INDEX}" '
                f'ON "{schema}"."{_TABLE}" '
                f"((data->>'channel_id'), (data->>'anchor'))"
            )
            async with self._sp.pool.acquire() as conn:
                await conn.execute(sql)
            self._unique_index_ensured = True
        elif backend == "sqlite":
            sql = (
                f'CREATE UNIQUE INDEX IF NOT EXISTS "{_UNIQUE_INDEX}" '
                f'ON "{_TABLE}" '
                "(json_extract(data, '$.channel_id'), "
                "json_extract(data, '$.anchor'))"
            )
            # _write_guard is taken for lock discipline AND correctness:
            # SQLite DDL is NOT immune to an already-open enclosing
            # transaction() - once that block's manual BEGIN is in effect,
            # this CREATE INDEX participates in it like any other statement
            # and rolls back with it. Mirrors _ensure_table's own pattern
            # (primer/storage/sqlite.py).
            async with self._sp._write_guard() as should_commit:  # noqa: SLF001
                await self._sp.connection.execute(sql)
                if should_commit:
                    await self._sp.connection.commit()
            # Only cache "ensured" when the DDL was actually COMMITTED, not
            # merely executed. Caching True on a deferred (should_commit=
            # False) run would skip re-creating the index the next time
            # this store is used, if the enclosing transaction() then rolls
            # back - the next upsert's INSERT...ON CONFLICT (unique-index
            # expression) fails outright since the index never actually
            # exists ("does not match any... constraint"; live-caught by
            # test_atomic_upsert_inside_a_rolled_back_transaction_is_undone).
            if should_commit:
                self._unique_index_ensured = True

    def _to_row(self, record: ChannelCorrelation) -> tuple[str, str]:
        """Dump a record to ``(id, data_json)`` -- mirrors the storage
        backends' own ``_to_row`` so the JSONB payload shape matches."""
        dumped = dump_for_storage(record)
        entity_id = dumped.pop("id")
        return entity_id, json.dumps(dumped)

    async def _atomic_upsert(self, record: ChannelCorrelation) -> ChannelCorrelation:
        """Insert *record*, or atomically update the existing row that shares
        its ``(channel_id, anchor)``.

        Returns the persisted record. On conflict the existing row's ``id`` is
        preserved (only ``data`` is replaced), so concurrent writers converge
        on a single row rather than racing to create two.
        """
        await self._ensure_unique_index()
        backend = self._backend()
        entity_id, data_json = self._to_row(record)
        if backend == "postgres":
            schema = self._sp.schema
            sql = (
                f'INSERT INTO "{schema}"."{_TABLE}" (id, data) '
                f"VALUES ($1, $2::jsonb) "
                f"ON CONFLICT ((data->>'channel_id'), (data->>'anchor')) "
                f"DO UPDATE SET data = EXCLUDED.data, updated_at = now() "
                f"RETURNING id, data"
            )
            async with self._sp.pool.acquire() as conn:
                row = await conn.fetchrow(sql, entity_id, data_json)
            data = row["data"]
            if isinstance(data, str):
                data = json.loads(data)
            data["id"] = row["id"]
            return ChannelCorrelation.model_validate(data)
        # sqlite
        sql = (
            f'INSERT INTO "{_TABLE}" (id, data) VALUES (?, ?) '
            "ON CONFLICT (json_extract(data, '$.channel_id'), "
            "json_extract(data, '$.anchor')) "
            "DO UPDATE SET data = excluded.data, updated_at = datetime('now') "
            "RETURNING id, data"
        )
        async with self._sp._write_guard() as should_commit:  # noqa: SLF001
            cur = await self._sp.connection.execute(sql, (entity_id, data_json))
            row = await cur.fetchone()
            if should_commit:
                await self._sp.connection.commit()
        data = json.loads(row[1])
        data["id"] = row[0]
        return ChannelCorrelation.model_validate(data)

    # ------------------------------------------------------------------
    # Write helpers
    # ------------------------------------------------------------------

    async def upsert_session(
        self,
        *,
        channel_id: str,
        anchor: str,
        workspace_id: str,
        session_id: str,
        tool_call_id: str,
    ) -> ChannelCorrelation:
        """Create or update a ``kind="session"`` correlation record.

        Atomic on (channel_id, anchor): two concurrent callers cannot create
        two rows for the same gate, so a parked session is never double-resumed
        by colliding correlations."""
        now = datetime.now(timezone.utc)
        record = ChannelCorrelation(
            channel_id=channel_id,
            anchor=anchor,
            kind="session",
            workspace_id=workspace_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
            updated_at=now,
        )
        if self._backend() == "other":
            return await self._fallback_upsert(record)
        return await self._atomic_upsert(record)

    async def upsert_thread_session(
        self,
        *,
        channel_id: str,
        anchor: str,
        workspace_id: str,
        session_id: str,
    ) -> ChannelCorrelation:
        """Bind a platform thread to its session (S6 section 5).

        ``tool_call_id`` is deliberately None: the record says "this thread
        IS this session" and nothing about an open gate. A later ask_user on
        the same anchor upgrades the SAME row with a tool_call_id; answering
        it clears the field again via :meth:`clear_gate`, so the thread stays
        mapped for the next reply.
        """
        now = datetime.now(timezone.utc)
        record = ChannelCorrelation(
            channel_id=channel_id,
            anchor=anchor,
            kind="session",
            workspace_id=workspace_id,
            session_id=session_id,
            tool_call_id=None,
            updated_at=now,
        )
        if self._backend() == "other":
            return await self._fallback_upsert(record)
        return await self._atomic_upsert(record)

    async def clear_gate(self, channel_id: str, anchor: str) -> None:
        """Blank a resolved gate's ``tool_call_id``, keeping the mapping.

        Without this the next reply in the thread would re-publish onto a
        resume key nobody is waiting on any more, instead of steering the
        mapped session.
        """
        existing = await self.lookup(channel_id, anchor)
        if existing is None or existing.tool_call_id is None:
            return
        await self._storage().update(
            existing.model_copy(update={
                "tool_call_id": None,
                "updated_at": datetime.now(timezone.utc),
            })
        )

    async def clear_for_session(self, session_id: str) -> int:
        """Delete every mapping pointing at ``session_id``. Returns the count.

        Called from the session DELETE path so a mapping never outlives its
        session (S6 section 9: mappings are garbage-collected with their
        sessions).
        """
        removed = 0
        while True:
            page = await self._storage().find(
                Q(ChannelCorrelation).where("session_id", session_id).build(),
                OffsetPage(offset=0, length=200),
            )
            if not page.items:
                return removed
            for record in page.items:
                await self._storage().delete(record.id)
                removed += 1
            if len(page.items) < 200:
                return removed

    async def _fallback_upsert(
        self, record: ChannelCorrelation
    ) -> ChannelCorrelation:
        """Read-modify-write upsert for storage backends without a raw
        connection (test doubles). Preserves the existing row's id."""
        existing = await self.lookup(record.channel_id, record.anchor)
        if existing is not None:
            update = {
                "kind": record.kind,
                "workspace_id": record.workspace_id,
                "session_id": record.session_id,
                "tool_call_id": record.tool_call_id,
                "updated_at": record.updated_at,
            }
            updated = existing.model_copy(update=update)
            await self._storage().update(updated)
            return updated
        await self._storage().create(record)
        return record

    async def list_for_channel(self, channel_id: str) -> list[ChannelCorrelation]:
        """Return all correlation records for *channel_id*.

        Pages internally using a window of 200 rows until exhausted.
        """
        results: list[ChannelCorrelation] = []
        offset = 0
        while True:
            page = await self._storage().find(
                Q(ChannelCorrelation).where("channel_id", channel_id).build(),
                OffsetPage(offset=offset, length=200),
            )
            results.extend(page.items)
            if len(page.items) < 200:
                break
            offset += 200
        return results

    async def clear(self, channel_id: str, anchor: str) -> None:
        """Delete the correlation record for *(channel_id, anchor)* if it exists."""
        existing = await self.lookup(channel_id, anchor)
        if existing is not None:
            await self._storage().delete(existing.id)


__all__ = ["CorrelationStore"]
