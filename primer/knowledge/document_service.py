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
from primer.model.except_ import NotFoundError


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
        """
        existing_id = await self._content.resolve_id(collection_id, path)
        doc_id = existing_id or _new_document_id()
        doc = Document(
            id=doc_id,
            collection_id=collection_id,
            name=title or _leaf(path),
            path=path,
            title=title,
            meta=meta or {},
        )
        async with self._sp.transaction() as conn:
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

        Raises :class:`NotFoundError` when no document lives at that path.
        """
        row = await self._content.get_by_path(collection_id, path)
        if row is None:
            raise NotFoundError(
                f"no document at path {path!r} in collection {collection_id!r}"
            )
        doc = await self._docs.get(row.document_id)
        if doc is None:
            # Content row without an entity row: a torn write the atomic
            # path is designed to prevent. Surface as not-found rather than
            # returning a half-populated result.
            raise NotFoundError(
                f"document entity {row.document_id!r} missing for path {path!r}"
            )
        return ReadResult(document=doc, content=row.content)

    async def list(
        self, *, collection_id: str, prefix: str | None = None
    ) -> list[ContentListEntry]:
        """List entries under an optional path prefix WITHOUT loading bodies.

        Delegates to the content store, whose entries carry ``path``,
        ``document_id`` and ``size`` (character length) but never the body.
        """
        return await self._content.list(collection_id, prefix=prefix)

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
