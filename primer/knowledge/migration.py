"""One-time, idempotent migration of legacy document bodies + paths.

Document bodies used to live under ``Document.meta['content']`` /
``['text']`` and documents had no ``path``. P1 moves bodies into the content
store (``provider.get_content_store()``) and makes ``path`` a REQUIRED field on
:class:`primer.model.collection.Document`.

This migration walks every document row and assigns a unique ``path`` derived
from ``name`` (with ``title=name``) onto the entity row, REGARDLESS of whether
its collection is system-managed. This is the critical step: system /catalog
collections (e.g. the AI-docs collection) DO persist Document *entity* rows, and
on an upgraded deployment those pre-existing rows have no ``path`` -- so they
would fail :class:`Document` validation on every load (bootstrap, vector
backfill, document listing) unless they too get a ``path``.

In ADDITION, for documents in a NON-system collection, the body is copied into
the content store under the assigned path. System-collection bodies stay where
they are (vector-backed); creating a content-store row for them is a P4 concern,
so we deliberately do NOT. The original ``meta`` is left intact -- deleting the
duplicated body is a later step.

CRITICAL: legacy rows have no ``path`` and would FAIL :class:`Document`
validation if loaded via ``Storage[Document]`` (which deserialises the JSON blob
into ``Document(**data)``). The migration therefore reads the legacy rows at the
RAW row level -- straight off the ``document`` JSON table on the provider's own
connection / pool -- and only constructs a valid ``Document`` once a path has
been derived.

The migration is idempotent and resumable: a document that already has a valid
``path`` (and, for non-system rows, a content row) is skipped, so re-running
(e.g. on every startup) is cheap and safe.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator

from primer.model.collection import Collection, Document
from primer.model.storage import OffsetPage


logger = logging.getLogger(__name__)


_SLUG_UNSAFE = re.compile(r"[^a-z0-9._-]+")

# How many raw document rows to pull into memory per batch. Keeps full legacy
# bodies (which still live under ``meta``) from accumulating all at once.
_READ_BATCH = 500


def _slugify_to_path(name: str) -> str:
    """Derive a filesystem-ish ``*.md`` path slug from a document name.

    Lowercases, collapses runs of unsafe characters (including spaces) to a
    single ``-``, strips leading/trailing ``-``/``.``/``/``, and appends
    ``.md``. The result is guaranteed to satisfy the :class:`Document` path
    validator (no leading/trailing slash, no empty/``.``/``..`` segments).
    """
    slug = _SLUG_UNSAFE.sub("-", name.strip().lower())
    slug = slug.strip("-./")
    # Guard against a slug that collapsed to '' or reserved dot segments.
    if not slug or slug in (".", ".."):
        slug = "document"
    return f"{slug}.md"


def _disambiguate(path: str, suffix: str) -> str:
    """Insert ``-<suffix>`` before the ``.md`` extension to break a collision."""
    if path.endswith(".md"):
        return f"{path[:-3]}-{suffix}.md"
    return f"{path}-{suffix}"


async def _collection_index(storage_provider) -> tuple[set[str], set[str]]:
    """Return ``(known_collection_ids, system_collection_ids)``.

    ``known_collection_ids`` is every collection that currently exists -- used
    to detect orphan documents (whose ``collection_id`` points at a deleted /
    missing collection). ``system_collection_ids`` is the subset marked
    ``system`` -- used to decide whether to copy the body into the content store
    (system bodies stay vector-backed).

    Collection is unchanged in P1 and still deserialises cleanly, so we can
    list it through ``Storage[Collection]``.
    """
    colls = storage_provider.get_storage(Collection)
    known_ids: set[str] = set()
    system_ids: set[str] = set()
    offset = 0
    page_len = 200
    while True:
        resp = await colls.list(OffsetPage(offset=offset, length=page_len))
        for c in resp.items:
            known_ids.add(c.id)
            if c.system:
                system_ids.add(c.id)
        if len(resp.items) < page_len:
            break
        offset += page_len
    return known_ids, system_ids


async def _iter_raw_documents(
    storage_provider,
) -> AsyncIterator[tuple[str, dict]]:
    """Yield every ``document`` row at the raw level as ``(id, data_dict)``.

    Bypasses ``Storage[Document]`` because legacy rows lack the now-required
    ``path`` field and would fail Document validation. Both backends store the
    entity in a ``document`` table with the body under a JSON column (``data``):

    * SQLite -- ``data`` is a JSON TEXT blob; parsed with ``json.loads``.
    * Postgres -- ``data`` is jsonb; asyncpg hands it back as text.

    Reads in batches of ``_READ_BATCH`` ordered by ``id`` so the full set of
    rows (which still carry legacy bodies under ``meta``) is never materialised
    in memory at once. Uses the provider's own connection / pool; opens nothing
    new. Yields nothing when the table does not exist yet (fresh install).
    """
    # SQLite provider exposes a single shared aiosqlite ``connection``.
    if hasattr(storage_provider, "connection"):
        conn = storage_provider.connection
        # Tolerate a fresh DB with no document table yet.
        cur = await conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name = 'document'"
        )
        if await cur.fetchone() is None:
            return
        last_id = ""
        while True:
            cur = await conn.execute(
                'SELECT id, data FROM "document" '
                "WHERE id > ? ORDER BY id ASC LIMIT ?",
                (last_id, _READ_BATCH),
            )
            rows = await cur.fetchall()
            if not rows:
                break
            for row in rows:
                data = (
                    json.loads(row[1]) if isinstance(row[1], str) else dict(row[1])
                )
                yield row[0], data
            last_id = rows[-1][0]
            if len(rows) < _READ_BATCH:
                break
        return

    # Postgres provider exposes an asyncpg ``pool`` + a ``schema``.
    if hasattr(storage_provider, "pool"):
        schema = storage_provider.schema
        async with storage_provider.pool.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT to_regclass($1)", f'"{schema}".document'
            )
            if exists is None:
                return
            last_id = ""
            while True:
                rows = await conn.fetch(
                    f'SELECT id, data FROM "{schema}".document '
                    "WHERE id > $1 ORDER BY id ASC LIMIT $2",
                    last_id,
                    _READ_BATCH,
                )
                if not rows:
                    break
                for row in rows:
                    data = row["data"]
                    if isinstance(data, str):
                        data = json.loads(data)
                    yield row["id"], dict(data)
                last_id = rows[-1]["id"]
                if len(rows) < _READ_BATCH:
                    break
        return

    # A provider that exposes neither a sqlite ``connection`` nor a postgres
    # ``pool`` (e.g. an in-memory test double) cannot hold legacy raw rows, so
    # there is nothing to migrate.
    logger.debug(
        "migrate_document_content: provider %s exposes no raw connection/pool; "
        "nothing to migrate",
        type(storage_provider).__name__,
    )
    return


async def migrate_document_content(storage_provider) -> int:
    """Backfill document ``path``s and copy legacy bodies into the content store.

    Two distinct concerns, deliberately separated:

    * **Path assignment (EVERY row, including system collections).** Any
      document row lacking a ``path`` is upgraded to a valid :class:`Document`
      (with a unique ``path`` derived from ``name`` and ``title=name``) and
      written back. This is required even for system-collection rows: they too
      persist Document entity rows, and on an upgraded deployment those rows
      have no ``path`` and would fail validation on every load.
    * **Body copy (NON-system collections only).** For non-system rows the body
      is additionally copied into the content store under the assigned path.
      System-collection bodies stay vector-backed (a P4 concern), so no
      content-store row is created for them.

    Orphan rows -- whose ``collection_id`` references a missing/deleted
    collection -- are logged and skipped rather than resurrected.

    Idempotent + resumable: a row that already has a valid ``path`` (and, for
    non-system rows, a content row) is passed over, so re-running is a no-op.

    Returns the number of NON-system document bodies copied on this run
    (path-only backfills of system rows are not counted).
    """
    content_store = storage_provider.get_content_store()
    docs_store = storage_provider.get_storage(Document)
    known_ids, system_ids = await _collection_index(storage_provider)

    # Track paths assigned within THIS run per collection so two same-name
    # documents do not collide before either content row is committed.
    assigned: dict[str, set[str]] = {}
    migrated = 0

    async for doc_id, data in _iter_raw_documents(storage_provider):
        collection_id = data.get("collection_id")
        name = data.get("name")
        meta = data.get("meta") or {}
        existing_path = data.get("path")
        if not collection_id or not name:
            logger.warning(
                "migrate_document_content: skipping document %r with missing "
                "collection_id/name",
                doc_id,
            )
            continue
        # Orphan guard: a document pointing at a missing/deleted collection is
        # not resurrected -- log + skip.
        if collection_id not in known_ids:
            logger.warning(
                "migrate_document_content: skipping orphan document %r -- "
                "collection %r does not exist",
                doc_id,
                collection_id,
            )
            continue

        is_system = collection_id in system_ids

        # Idempotency. A system row is done once it has a path (no content row
        # is ever expected). A non-system row is done once it has both a path
        # and a content row.
        if existing_path:
            if is_system or await content_store.get(doc_id) is not None:
                continue

        # Derive a unique path for rows that lack one; reuse an existing path.
        taken = assigned.setdefault(collection_id, set())
        if existing_path:
            path = existing_path
        else:
            path = _slugify_to_path(name)
            if path in taken or await content_store.resolve_id(
                collection_id, path
            ):
                path = _disambiguate(path, doc_id[:6])
                # Extremely unlikely second collision; widen the suffix.
                while path in taken or await content_store.resolve_id(
                    collection_id, path
                ):
                    path = _disambiguate(_slugify_to_path(name), doc_id)

        doc = Document(
            id=doc_id,
            collection_id=collection_id,
            name=name,
            path=path,
            title=name,
            meta=meta,
        )

        async with storage_provider.transaction() as conn:
            # Body copy is for non-system collections only; system bodies stay
            # vector-backed (P4).
            if not is_system:
                body = meta.get("content") or meta.get("text") or ""
                await content_store.upsert(
                    document_id=doc_id,
                    collection_id=collection_id,
                    path=path,
                    content=body,
                    conn=conn,
                )
            await docs_store.update(doc, conn=conn)

        taken.add(path)
        if not is_system:
            migrated += 1

    if migrated:
        logger.info("migrate_document_content: migrated %d document(s)", migrated)
    return migrated


__all__ = ["migrate_document_content"]
