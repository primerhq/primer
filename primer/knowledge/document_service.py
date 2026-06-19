"""Path-addressed document service.

:class:`DocumentService` owns the create / read / list / delete / move
surface for path-addressed documents. A document is two rows that must
stay in lockstep:

* the :class:`~primer.model.collection.Document` *entity* row (JSONB
  metadata: ``collection_id``, ``name``, ``path`` mirror, ``title``,
  ``meta``) held by the model-bound :class:`~primer.int.Storage`, and
* the *content* row (the body, keyed by the stable document id, with a
  ``UNIQUE(collection_id, path)`` constraint) held by the
  :class:`~primer.int.document_content.DocumentContentStore`.

The content store is authoritative for path<->id resolution and path
uniqueness; the entity carries a ``path`` mirror for display and entity
queries. Every mutation writes BOTH rows inside ONE backend transaction
(via ``StorageProvider.transaction()``) so a failure in either write
leaves no committed orphan: either both rows land or neither does.

Search indexing (P1 keeps search on) is an optional, best-effort hook:
when an ``indexer`` callable is supplied it is invoked AFTER a successful
write so the entity + body are durable before any embedding work begins.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Protocol

from pydantic import BaseModel

from primer.int.document_content import ContentListEntry, DocumentContentStore
from primer.int.storage import Storage
from primer.model.collection import Document
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.storage import OffsetPage
from primer.storage.q import Q


# Optional best-effort search-indexing hook. Called with the freshly
# persisted document + its body after a successful upsert.
Indexer = Callable[..., Awaitable[None]]


class _TransactionalProvider(Protocol):
    """The slice of ``StorageProvider`` the service relies on.

    Both concrete providers (sqlite + postgres) implement ``transaction()``
    as an async context manager yielding a backend connection to thread
    through the entity + content writes. Pool-less SQLite ignores the
    yielded value beyond suppressing its per-write commits; Postgres runs
    every threaded write inside the one acquired transaction.
    """

    def get_storage(self, model_class: type[Document]) -> Storage[Document]: ...

    def get_content_store(self) -> DocumentContentStore: ...

    def transaction(self) -> Any: ...


class ReadResult(BaseModel):
    """A document body together with its entity metadata."""

    document: Document
    content: str


def _leaf(path: str) -> str:
    """Return the final path segment (the filename), used as the default name."""
    return path.rsplit("/", 1)[-1]


def _new_document_id() -> str:
    """Mint a fresh ``document-<hex>`` id.

    Reuses the :class:`~primer.model.common.Identifiable` autogeneration at
    its single chokepoint: constructing a ``Document`` with no ``id`` runs
    the ``_assign_id`` model validator, which stamps ``document-<hex12>``.
    """
    # Minimal placeholder fields; only the generated id is consumed.
    return Document(
        collection_id="_", name="_", path="_"
    ).id  # type: ignore[return-value]


class DocumentService:
    """Path-addressed create/read/list/delete/move over entity + content rows.

    Writes the :class:`Document` entity row and the body row in ONE
    transaction so the two never diverge. The content store is the
    authority for ``(collection_id, path) -> document_id`` resolution.
    """

    def __init__(self, storage_provider: _TransactionalProvider, *, indexer: Indexer | None = None) -> None:
        self._sp = storage_provider
        self._docs: Storage[Document] = storage_provider.get_storage(Document)
        self._content: DocumentContentStore = storage_provider.get_content_store()
        # Optional async callable(document=..., content=...) invoked after a
        # successful write to (re)index the body for search. ``None`` disables
        # indexing (the unit-test / search-off configuration).
        self._indexer = indexer

    async def upsert(
        self,
        *,
        collection_id: str,
        path: str,
        content: str,
        title: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Document:
        """Create or replace the document at ``(collection_id, path)``.

        Resolves the existing document id (so repeated upserts on the same
        path update the same entity rather than minting a new one), writes
        the entity + body atomically, then runs the optional indexer hook.
        Returns the stored :class:`Document`.

        Concurrency: ``resolve_id`` runs INSIDE the transaction so the
        create-vs-update decision is made under the same write scope as the
        body write (on sqlite the provider's write-lock serialises it; on
        postgres it shares the ``conn.transaction()``). If two upserts for the
        same path still race past the resolve, the second's content ``upsert``
        hits ``UNIQUE(collection_id, path)`` and raises
        :class:`ConflictError`; we catch it, re-resolve the now-existing
        document id, and retry once as an UPDATE so both callers converge to a
        single document with no 500.
        """
        try:
            return await self._upsert_once(
                collection_id=collection_id,
                path=path,
                content=content,
                title=title,
                meta=meta,
            )
        except ConflictError:
            # Lost the create race: another upsert took this path. Re-resolve
            # and retry as an update against the now-existing document id.
            return await self._upsert_once(
                collection_id=collection_id,
                path=path,
                content=content,
                title=title,
                meta=meta,
            )

    async def _upsert_once(
        self,
        *,
        collection_id: str,
        path: str,
        content: str,
        title: str | None,
        meta: dict[str, Any] | None,
    ) -> Document:
        async with self._sp.transaction() as conn:
            existing_id = await self._content.resolve_id(
                collection_id, path, conn=conn
            )
            doc_id = existing_id or _new_document_id()
            doc = Document(
                id=doc_id,
                collection_id=collection_id,
                name=title or _leaf(path),
                path=path,
                title=title,
                meta=meta or {},
            )
            if existing_id is None:
                await self._docs.create(doc, conn=conn)
            else:
                await self._docs.update(doc, conn=conn)
            await self._content.upsert(
                document_id=doc_id,
                collection_id=collection_id,
                path=path,
                content=content,
                conn=conn,
            )
        if self._indexer is not None:
            await self._indexer(document=doc, content=content)
        return doc

    async def read(self, *, collection_id: str, path: str) -> ReadResult:
        """Return the body + entity for ``(collection_id, path)``.

        The content store is the authority, but documents created through
        the generic CRUD route (``POST/PUT /v1/documents``) write only the
        :class:`Document` ENTITY row (body in ``meta``) and NO content row.
        Those docs still carry a valid ``path`` on the entity, so when the
        content store has no row we FALL BACK to an entity lookup by
        ``(collection_id, path)`` and serve the body from ``meta`` via
        :func:`document_body_text`. This keeps the path surface from hiding
        a real, searchable document.

        Raises :class:`NotFoundError` only when neither a content row NOR an
        entity row exists at that path.
        """
        row = await self._content.get_by_path(collection_id, path)
        if row is not None:
            doc = await self._docs.get(row.document_id)
            if doc is None:
                # Content row without an entity row: a torn write the atomic
                # path is designed to prevent. Surface as not-found rather
                # than returning a half-populated result.
                raise NotFoundError(
                    f"document entity {row.document_id!r} missing for path {path!r}"
                )
            return ReadResult(document=doc, content=row.content)

        # No content row: fall back to an entity-only document mirroring the
        # path (created via the generic CRUD route). Serve its meta body.
        doc = await self._find_entity_by_path(collection_id, path)
        if doc is None:
            raise NotFoundError(
                f"no document at path {path!r} in collection {collection_id!r}"
            )
        from primer.knowledge.indexing import document_body_text

        return ReadResult(document=doc, content=document_body_text(doc))

    async def _find_entity_by_path(
        self, collection_id: str, path: str
    ) -> Document | None:
        """Find the Document ENTITY at ``(collection_id, path)``, or None.

        Used as the fallback for entity-only documents (generic-CRUD writes
        that never created a content row). The entity carries a ``path``
        mirror, so a typed ``Q`` predicate resolves it.
        """
        predicate = (
            Q(Document)
            .where("collection_id", collection_id)
            .where("path", path)
            .build()
        )
        response = await self._docs.find(predicate, OffsetPage(offset=0, length=1))
        items = response.items
        return items[0] if items else None

    async def list(
        self, *, collection_id: str, prefix: str | None = None
    ) -> list[ContentListEntry]:
        """List entries under an optional path prefix WITHOUT loading bodies.

        UNION of two sources, deduped by path with the content row WINNING:

        * the content store (authoritative), whose entries carry ``path``,
          ``document_id`` and ``size`` (character length), and
        * Document ENTITY rows that carry a ``path`` but have NO content row
          (created via the generic CRUD route). These are surfaced so an
          entity-only document is never hidden from the path listing; their
          ``size`` is the meta body length.

        Never loads a content-store body; entity ``size`` comes from the
        cheap meta body length already on the entity row.
        """
        entries = await self._content.list(collection_id, prefix=prefix)
        seen_paths = {e.path for e in entries}

        # Find entity rows for this collection with a path; include only
        # those whose path is not already covered by a content row.
        predicate = Q(Document).where("collection_id", collection_id).build()
        from primer.knowledge.indexing import document_body_text

        result = list(entries)
        offset = 0
        page_size = 200
        while True:
            response = await self._docs.find(
                predicate, OffsetPage(offset=offset, length=page_size)
            )
            items = response.items
            for doc in items:
                if not doc.path or doc.path in seen_paths:
                    continue
                if prefix and not doc.path.startswith(prefix):
                    continue
                seen_paths.add(doc.path)
                result.append(
                    ContentListEntry(
                        document_id=doc.id,
                        path=doc.path,
                        size=len(document_body_text(doc)),
                    )
                )
            if len(items) < page_size:
                break
            offset += page_size
        return result

    async def delete(self, *, collection_id: str, path: str) -> None:
        """Delete the entity + body at ``(collection_id, path)`` atomically.

        Raises :class:`NotFoundError` when no document lives at that path.
        """
        doc_id = await self._content.resolve_id(collection_id, path)
        if doc_id is None:
            raise NotFoundError(
                f"no document at path {path!r} in collection {collection_id!r}"
            )
        async with self._sp.transaction() as conn:
            await self._docs.delete(doc_id, conn=conn)
            await self._content.delete(doc_id, conn=conn)

    async def move(self, *, collection_id: str, src: str, dst: str) -> None:
        """Move the document from ``src`` to ``dst`` within a collection.

        Updates the authoritative content-store path and the entity's path
        mirror in ONE transaction. ``DocumentContentStore.move`` raises
        :class:`~primer.model.except_.ConflictError` if ``dst`` is already
        taken; that aborts the transaction so the entity path is unchanged.
        Raises :class:`NotFoundError` when ``src`` does not exist.
        """
        doc_id = await self._content.resolve_id(collection_id, src)
        if doc_id is None:
            raise NotFoundError(
                f"no document at path {src!r} in collection {collection_id!r}"
            )
        async with self._sp.transaction() as conn:
            # Move the content row first: it owns the UNIQUE(collection_id,
            # path) constraint, so a collision raises ConflictError here and
            # aborts before the entity mirror is touched.
            await self._content.move(doc_id, dst, conn=conn)
            doc = await self._docs.get(doc_id, conn=conn)
            if doc is None:
                raise NotFoundError(
                    f"document entity {doc_id!r} missing for path {src!r}"
                )
            moved = doc.model_copy(update={"path": dst})
            await self._docs.update(moved, conn=conn)


__all__ = ["DocumentService", "ReadResult"]
