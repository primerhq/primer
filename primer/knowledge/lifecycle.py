"""Per-collection semantic-search lifecycle: enable/backfill/disable/status."""
from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel

from primer.knowledge.indexing import index_documents
from primer.model.collection import Collection, CollectionSearchConfig, Document
from primer.model.except_ import ConflictError, NotFoundError, PrimerError, ProviderError
from primer.model.provider import (
    CrossEncoderProvider, EmbeddingProvider, SemanticSearchProvider,
)
from primer.model.storage import OffsetPage
from primer.storage.q import Q

logger = logging.getLogger(__name__)


class SearchStatus(BaseModel):
    state: Literal["ready", "indexing", "error", "disabled"]
    error: str | None = None
    documents_total: int = 0
    documents_indexed: int = 0


async def validate_search_config(
    storage_provider, cfg: CollectionSearchConfig
) -> None:
    emb = await storage_provider.get_storage(EmbeddingProvider).get(
        cfg.embedder.provider_id
    )
    if emb is None:
        raise ConflictError(
            f"embedding provider {cfg.embedder.provider_id!r} is not "
            "registered; register it under /v1/embedding_providers (extras "
            "may be required, see GET /v1/capabilities) and retry"
        )
    ssp = await storage_provider.get_storage(SemanticSearchProvider).get(
        cfg.vector_store_provider_id
    )
    if ssp is None:
        raise ConflictError(
            f"semantic search provider {cfg.vector_store_provider_id!r} is "
            "not registered; register it under /v1/ssp and retry"
        )
    if cfg.cross_encoder is not None:
        ce = await storage_provider.get_storage(CrossEncoderProvider).get(
            cfg.cross_encoder.provider_id
        )
        if ce is None:
            raise ConflictError(
                f"cross-encoder provider {cfg.cross_encoder.provider_id!r} "
                "is not registered; register it under "
                "/v1/cross_encoder_providers and retry"
            )


async def _collection_documents(
    storage_provider, collection_id: str
) -> list[Document]:
    docs = storage_provider.get_storage(Document)
    predicate = Q(Document).where("collection_id", collection_id).build()
    out: list[Document] = []
    offset, page = 0, 200
    while True:
        resp = await docs.find(predicate, OffsetPage(offset=offset, length=page))
        out.extend(resp.items)
        if len(resp.items) < page:
            return out
        offset += page


async def _write_search(storage_provider, collection: Collection,
                        cfg: CollectionSearchConfig | None) -> Collection:
    updated = collection.model_copy(update={"search": cfg})
    return await storage_provider.get_storage(Collection).update(updated)


async def enable_search(storage_provider, provider_registry, ssr, *,
                        collection_id: str,
                        cfg: CollectionSearchConfig) -> Collection:
    colls = storage_provider.get_storage(Collection)
    collection = await colls.get(collection_id)
    if collection is None:
        raise NotFoundError(f"Collection {collection_id!r} does not exist")
    await validate_search_config(storage_provider, cfg)
    collection = await _write_search(
        storage_provider, collection,
        cfg.model_copy(update={"state": "indexing", "error": None}),
    )
    content_store = storage_provider.get_content_store()
    try:
        # Batched across documents, not one call per document. Every
        # document here shares the collection's embedder, so the
        # per-document form spent one probe and one embed round-trip
        # each: on a collection the size of the system map that ran to
        # several hundred calls, enough to put a bootstrap past the e2e
        # lane's per-test ceiling.
        await index_documents(
            documents=await _collection_documents(
                storage_provider, collection_id,
            ),
            collection=collection,
            provider_registry=provider_registry,
            semantic_search_registry=ssr,
            content_store=content_store,
        )
    except Exception as exc:  # noqa: BLE001 - partial index stays intact
        logger.exception("backfill failed for collection %s", collection_id)
        return await _write_search(
            storage_provider, collection,
            cfg.model_copy(update={"state": "error", "error": str(exc)}),
        )
    return await _write_search(
        storage_provider, collection,
        cfg.model_copy(update={"state": "ready", "error": None}),
    )


async def disable_search(storage_provider, ssr, *, collection_id: str) -> Collection:
    colls = storage_provider.get_storage(Collection)
    collection = await colls.get(collection_id)
    if collection is None:
        raise NotFoundError(f"Collection {collection_id!r} does not exist")
    if collection.search is not None:
        try:
            store = await ssr.get_store(collection.search.vector_store_provider_id)
            await store.drop_collection(collection_id)
        except PrimerError:
            pass  # provider gone or namespace absent: disable always works
        except Exception:  # noqa: BLE001
            logger.exception("dropping vectors for %s failed", collection_id)
    return await _write_search(storage_provider, collection, None)


async def _drop_vector_namespace(ssr, collection: Collection) -> None:
    """Drop the collection's vector namespace, FAILING CLOSED (unlike :func:`disable_search`, which always succeeds).

    A namespace left behind is not just clutter: the next collection created under the same id finds the old chunks waiting. So only
    "there is nothing to drop" is tolerated: no search configured, no registry in this process, or the provider row / namespace is
    gone (:class:`NotFoundError`). Anything else (a network error, a store that raises, a provider that cannot be built) is reported
    as a :class:`ProviderError` that names the way out: disable the collection's search first, which always works, then delete.
    """
    if collection.search is None:
        return
    if ssr is None:
        logger.warning(
            "collection %s has search configured but this process has no semantic-search registry; its vectors are not dropped",
            collection.id,
        )
        return
    try:
        store = await ssr.get_store(collection.search.vector_store_provider_id)
        await store.drop_collection(collection.id)
    except NotFoundError:
        return
    except Exception as exc:  # noqa: BLE001 - any other failure must stop the delete, not be swallowed
        raise ProviderError(
            f"could not drop the vectors of collection {collection.id!r} from vector store provider "
            f"{collection.search.vector_store_provider_id!r} ({type(exc).__name__}: {exc}); nothing was deleted. "
            f"Retry once the store is reachable, or disable the collection's search first "
            f"(DELETE /v1/collections/{collection.id}/search always works) and delete it again.",
            cause=exc,
        ) from exc


# One transaction per batch: a collection of any size is removed without one unbounded transaction, and a failure part-way leaves a
# smaller collection that deleting again finishes (every step is idempotent). Matches the 200-row page the rest of this module reads.
_PURGE_BATCH = 200

# How many times the purge empties the collection, drops the namespace and looks again before it gives up on a writer that keeps adding
# documents. One round is enough unless something writes during the delete; three is generous for a burst and still bounded.
_PURGE_ROUNDS = 3


async def _delete_documents(storage_provider, collection_id: str) -> int:
    """Remove the collection's documents and their content rows, batch by batch; returns how many documents were removed."""
    docs = storage_provider.get_storage(Document)
    content = storage_provider.get_content_store()
    predicate = Q(Document).where("collection_id", collection_id).build()
    removed = 0
    previous_first: str | None = None
    while True:
        # Always the first page: the rows of the previous batch are gone, so offset 0 is the next batch.
        page = await docs.find(predicate, OffsetPage(offset=0, length=_PURGE_BATCH))
        if not page.items:
            return removed
        if page.items[0].id == previous_first:
            # The batch we just deleted is back: a delete that removes nothing would otherwise loop here for ever. Not a ProviderError:
            # that reads as a 502 blaming the vector store, and this is our own storage.
            raise PrimerError(
                f"deleting the documents of collection {collection_id!r} made no progress (document {previous_first!r} is still "
                "there after its batch was deleted); the collection was not deleted"
            )
        previous_first = page.items[0].id
        async with storage_provider.transaction() as conn:
            for document in page.items:
                try:
                    await docs.delete(document.id, conn=conn)
                except NotFoundError:
                    pass  # someone else deleted it between our read and this delete: it is gone, which is what we want
                await content.delete(document.id, conn=conn)
        removed += len(page.items)


async def _has_documents(storage_provider, collection_id: str) -> bool:
    page = await storage_provider.get_storage(Document).find(
        Q(Document).where("collection_id", collection_id).build(), OffsetPage(offset=0, length=1),
    )
    return bool(page.items)


async def purge_collection(storage_provider, ssr, *, collection: Collection) -> int:
    """Remove everything a collection owns except its own row; returns how many documents were removed.

    Called BEFORE the collection row is deleted (the REST ``on_pre_delete`` hook and the system tool's ``pre_delete``), so the row is
    the last thing to go and a failure part-way leaves a collection the caller can delete again, never documents without a collection.
    Order: the vector namespace first (:func:`_drop_vector_namespace`, one call whatever the size, and the step that can fail for a
    reason outside the database), then the documents with their content rows, batch by batch.

    A write that was in flight when the namespace was first dropped (an indexing pass of a PUT, say) can recreate the namespace and put
    chunks after it, and a document can be created while the loop runs. So after the documents the namespace is dropped AGAIN and the
    documents are looked at once more, for up to :data:`_PURGE_ROUNDS` rounds; a writer that outlasts them gets a
    :class:`~primer.model.except_.ConflictError` and the collection is left whole for a later retry. The content rows are swept last, by
    collection id, so a row with no document entity cannot keep a path of the reused id taken. What this does NOT close: a write that
    lands in the instant between that last look and the row delete by the caller; it would need a tombstone state on the collection that
    refuses writes while it is being deleted (ticketed, not built).

    After a failure part-way the vector namespace is already gone, so search on the collection finds nothing until the delete is
    retried; the rows that remain are intact.

    Synchronous on purpose: collections are text-only and bounded (a document body is capped at 1 MiB), the vector side is one call, and
    the caller learns the outcome from the response, as the search backfill (``enable_search``) already does.
    """
    await _drop_vector_namespace(ssr, collection)
    removed = 0
    for _ in range(_PURGE_ROUNDS):
        removed += await _delete_documents(storage_provider, collection.id)
        await _drop_vector_namespace(ssr, collection)
        if not await _has_documents(storage_provider, collection.id):
            await storage_provider.get_content_store().delete_collection(collection.id)
            return removed
    raise ConflictError(
        f"documents keep being added to collection {collection.id!r} while it is deleted; stop whatever is writing to it and delete it again "
        "(the documents already removed stay removed, and its vector index is empty until then)"
    )


async def search_status(storage_provider, ssr, *, collection_id: str) -> SearchStatus:
    colls = storage_provider.get_storage(Collection)
    collection = await colls.get(collection_id)
    if collection is None:
        raise NotFoundError(f"Collection {collection_id!r} does not exist")
    docs = await _collection_documents(storage_provider, collection_id)
    if collection.search is None:
        return SearchStatus(state="disabled", documents_total=len(docs))
    indexed = 0
    try:
        store = await ssr.get_store(collection.search.vector_store_provider_id)
        records = await store.search_by_meta(collection_id, meta={})
        indexed = len({r.document_id for r in records})
    except Exception:  # noqa: BLE001 - status is best-effort
        indexed = 0
    return SearchStatus(
        state=collection.search.state, error=collection.search.error,
        documents_total=len(docs), documents_indexed=indexed,
    )


__all__ = [
    "SearchStatus", "disable_search", "enable_search", "purge_collection",
    "search_status", "validate_search_config",
]
