"""One-time, idempotent migration of legacy document bodies + paths.

Document bodies used to live under ``Document.meta['content']`` /
``['text']`` and documents had no ``path``. P1 moves bodies into the content
store (``provider.get_content_store()``) and makes ``path`` a REQUIRED field on
:class:`primer.model.collection.Document`.

This migration walks every document in a NON-system collection, copies its body
into the content store, and assigns a unique ``path`` derived from ``name`` (with
``title=name``). System / catalog collections are skipped (they are a P4
concern). The original ``meta`` is left intact -- deleting the duplicated body
is a later step.

CRITICAL: legacy rows have no ``path`` and would FAIL :class:`Document`
validation if loaded via ``Storage[Document]`` (which deserialises the JSON blob
into ``Document(**data)``). The migration therefore reads the legacy rows at the
RAW row level -- straight off the ``document`` JSON table on the provider's own
connection / pool -- and only constructs a valid ``Document`` once a path has
been derived.

The migration is idempotent and resumable: a document whose content row already
exists is skipped, so re-running (e.g. on every startup) is cheap and safe.
"""

from __future__ import annotations

import json
import logging
import re

from primer.model.collection import Collection, Document
from primer.model.storage import OffsetPage


logger = logging.getLogger(__name__)


_SLUG_UNSAFE = re.compile(r"[^a-z0-9._-]+")


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


async def _iter_system_collection_ids(storage_provider) -> set[str]:
    """Return the ids of every system collection (these are skipped).

    Collection is unchanged in P1 and still deserialises cleanly, so we can
    list it through ``Storage[Collection]``.
    """
    colls = storage_provider.get_storage(Collection)
    system_ids: set[str] = set()
    offset = 0
    page_len = 200
    while True:
        resp = await colls.list(OffsetPage(offset=offset, length=page_len))
        for c in resp.items:
            if c.system:
                system_ids.add(c.id)
        if len(resp.items) < page_len:
            break
        offset += page_len
    return system_ids


async def _read_raw_documents(storage_provider) -> list[tuple[str, dict]]:
    """Read every ``document`` row at the raw level as ``(id, data_dict)``.

    Bypasses ``Storage[Document]`` because legacy rows lack the now-required
    ``path`` field and would fail Document validation. Both backends store the
    entity in a ``document`` table with the body under a JSON column (``data``):

    * SQLite -- ``data`` is a JSON TEXT blob; parsed with ``json.loads``.
    * Postgres -- ``data`` is jsonb; asyncpg hands it back as text.

    Uses the provider's own connection / pool; opens nothing new. Returns ``[]``
    when the table does not exist yet (fresh install, nothing to migrate).
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
            return []
        cur = await conn.execute('SELECT id, data FROM "document"')
        rows = await cur.fetchall()
        out: list[tuple[str, dict]] = []
        for row in rows:
            data = json.loads(row[1]) if isinstance(row[1], str) else dict(row[1])
            out.append((row[0], data))
        return out

    # Postgres provider exposes an asyncpg ``pool`` + a ``schema``.
    if hasattr(storage_provider, "pool"):
        schema = storage_provider.schema
        async with storage_provider.pool.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT to_regclass($1)", f'"{schema}".document'
            )
            if exists is None:
                return []
            rows = await conn.fetch(f'SELECT id, data FROM "{schema}".document')
        out = []
        for row in rows:
            data = row["data"]
            if isinstance(data, str):
                data = json.loads(data)
            out.append((row["id"], dict(data)))
        return out

    # A provider that exposes neither a sqlite ``connection`` nor a postgres
    # ``pool`` (e.g. an in-memory test double) cannot hold legacy raw rows, so
    # there is nothing to migrate.
    logger.debug(
        "migrate_document_content: provider %s exposes no raw connection/pool; "
        "nothing to migrate",
        type(storage_provider).__name__,
    )
    return []


async def migrate_document_content(storage_provider) -> int:
    """Copy legacy document bodies + assign paths into the content store.

    For each document in a non-system collection that has not already been
    migrated, writes its body into the content store under a unique path
    derived from ``name`` and upgrades the entity row to a valid
    :class:`Document` (with ``path`` and ``title=name``). System collections
    are skipped. Idempotent + resumable: documents whose content row already
    exists are passed over, so re-running is a no-op.

    Returns the number of documents migrated on this run.
    """
    content_store = storage_provider.get_content_store()
    docs_store = storage_provider.get_storage(Document)
    system_ids = await _iter_system_collection_ids(storage_provider)
    raw = await _read_raw_documents(storage_provider)

    # Track paths assigned within THIS run per collection so two same-name
    # documents do not collide before either content row is committed.
    assigned: dict[str, set[str]] = {}
    migrated = 0

    for doc_id, data in raw:
        collection_id = data.get("collection_id")
        name = data.get("name")
        meta = data.get("meta") or {}
        if not collection_id or not name:
            logger.warning(
                "migrate_document_content: skipping document %r with missing "
                "collection_id/name",
                doc_id,
            )
            continue
        if collection_id in system_ids:
            continue
        # Idempotency: already migrated (content row exists) -> skip.
        if await content_store.get(doc_id) is not None:
            continue

        body = meta.get("content") or meta.get("text") or ""

        taken = assigned.setdefault(collection_id, set())
        path = _slugify_to_path(name)
        if path in taken or await content_store.resolve_id(collection_id, path):
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
            await content_store.upsert(
                document_id=doc_id,
                collection_id=collection_id,
                path=path,
                content=body,
                conn=conn,
            )
            await docs_store.update(doc, conn=conn)

        taken.add(path)
        migrated += 1

    if migrated:
        logger.info("migrate_document_content: migrated %d document(s)", migrated)
    return migrated


__all__ = ["migrate_document_content"]
