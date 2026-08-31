"""Per-collection semantic-search lifecycle: enable/backfill/disable/status."""
from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel

from primer.knowledge.indexing import index_documents
from primer.model.collection import Collection, CollectionSearchConfig, Document
from primer.model.except_ import ConflictError, NotFoundError, PrimerError
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
    "SearchStatus", "disable_search", "enable_search",
    "search_status", "validate_search_config",
]
