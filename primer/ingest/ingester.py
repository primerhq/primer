"""The :class:`DocumentIngester` orchestrator.

Drives the full pipeline for one document:

1. Validate ``document.collection_id`` matches the bound collection.
2. Optionally delete existing chunks for re-ingestion.
3. Load the source via the bound :class:`DocumentLoader`.
4. Split the loaded document via the bound :class:`DocumentSplitter`.
5. Embed the first chunk alone to learn the vector dimensionality.
6. Lazy-create the vector-store collection with that dimension.
7. Put the first record; embed remaining chunks in batches; put each.
8. Return an :class:`IngestResult` with telemetry.

See ``docs/superpowers/specs/2026-05-03-document-ingestion-design.md``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from primer.ingest.loader import DocumentLoader
from primer.ingest.splitter import DocumentSplitter
from primer.model.chat import TextPart
from primer.model.except_ import BadRequestError
from primer.model.ingest import Chunk, IngestResult
from primer.model.vector import EmbeddingRecord


if TYPE_CHECKING:
    from primer.int.embedder import Embedder
    from primer.int.vector_store import VectorStore
    from primer.model.collection import Collection, Document


logger = logging.getLogger(__name__)


class DocumentIngester:
    """Orchestrate ``load -> split -> embed -> store`` for one document."""

    DEFAULT_BATCH_SIZE = 32

    def __init__(
        self,
        *,
        collection: "Collection",
        embedder: "Embedder",
        vector_store: "VectorStore",
        loader: DocumentLoader | None = None,
        splitter: DocumentSplitter | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        distance: Literal["cosine", "l2", "ip"] = "cosine",
    ) -> None:
        if batch_size <= 0:
            raise ValueError(f"batch_size must be > 0, got {batch_size!r}")
        if loader is None:
            # Route through the lazy package re-export so a missing 'docling'
            # extra surfaces the install hint rather than a bare import error.
            from primer.ingest.loaders import DoclingLoader

            loader = DoclingLoader()
        if splitter is None:
            from primer.ingest.splitters import DoclingSplitter

            splitter = DoclingSplitter()
        self._collection = collection
        self._embedder = embedder
        self._vector_store = vector_store
        self._loader = loader
        self._splitter = splitter
        self._batch_size = batch_size
        self._distance: Literal["cosine", "l2", "ip"] = distance

    @property
    def collection(self) -> "Collection":
        return self._collection

    async def ingest(
        self,
        document: "Document",
        source: bytes | Path | str,
        *,
        replace: bool = False,
    ) -> IngestResult:
        """Run the full pipeline for one document."""
        if document.collection_id != self._collection.id:
            raise BadRequestError(
                f"document.collection_id {document.collection_id!r} does not "
                f"match the ingester's collection {self._collection.id!r}"
            )

        if replace:
            await self._vector_store.delete(self._collection.id, document.id)

        loaded = await self._loader.load(source)
        chunks = self._splitter.split(loaded)
        if not chunks:
            return IngestResult(
                collection_id=self._collection.id,
                document_id=document.id,
                chunks_indexed=0,
                dimensions=1,  # placeholder; never used because zero chunks
                replaced=replace,
                bytes_loaded=loaded.meta.get("bytes_loaded"),
            )

        # Embed first chunk to learn dimensionality, then create collection.
        first_vector = await self._embed_one(chunks[0].text)
        dimensions = len(first_vector)
        if dimensions == 0:
            raise BadRequestError(
                "embedder returned a zero-length vector for the first "
                "chunk; cannot create vector-store collection"
            )

        await self._vector_store.create_collection(
            self._collection.id,
            dimensions=dimensions,
            distance=self._distance,
        )

        await self._vector_store.put(
            self._make_record(
                document=document,
                chunk=chunks[0],
                vector=first_vector,
                source_doc_meta=loaded.meta,
            )
        )

        # Batch-embed remaining chunks.
        remaining = chunks[1:]
        for batch_start in range(0, len(remaining), self._batch_size):
            batch = remaining[batch_start : batch_start + self._batch_size]
            vectors = await self._embed_batch([c.text for c in batch])
            for chunk, vector in zip(batch, vectors, strict=True):
                if len(vector) != dimensions:
                    raise BadRequestError(
                        f"embedder returned vector of dimension {len(vector)}, "
                        f"expected {dimensions} (consistent with first chunk)"
                    )
                await self._vector_store.put(
                    self._make_record(
                        document=document,
                        chunk=chunk,
                        vector=vector,
                        source_doc_meta=loaded.meta,
                    )
                )

        return IngestResult(
            collection_id=self._collection.id,
            document_id=document.id,
            chunks_indexed=len(chunks),
            dimensions=dimensions,
            replaced=replace,
            bytes_loaded=loaded.meta.get("bytes_loaded"),
        )

    # ---- Internals -------------------------------------------------------

    async def _embed_one(self, text: str) -> list[float]:
        """Embed a single text and return its vector."""
        response = await self._embedder.embed(
            model=self._collection.embedder.model,
            inputs=[TextPart(text=text)],
        )
        if not response.embeddings:
            raise BadRequestError("embedder returned no embeddings for one input")
        return list(response.embeddings[0].vector)

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts; preserve input order."""
        response = await self._embedder.embed(
            model=self._collection.embedder.model,
            inputs=[TextPart(text=t) for t in texts],
        )
        if len(response.embeddings) != len(texts):
            raise BadRequestError(
                f"embedder returned {len(response.embeddings)} embeddings "
                f"for {len(texts)} inputs"
            )
        return [list(emb.vector) for emb in response.embeddings]

    @staticmethod
    def _make_record(
        *,
        document: "Document",
        chunk: Chunk,
        vector: list[float],
        source_doc_meta: dict,
    ) -> EmbeddingRecord:
        merged_meta: dict = dict(chunk.meta)
        if source_doc_meta:
            merged_meta["source_doc"] = dict(source_doc_meta)
        return EmbeddingRecord(
            collection_id=document.collection_id,
            document_id=document.id,
            chunk_id=f"chunk-{chunk.position:06d}",
            text=chunk.text,
            vector=vector,
            meta=merged_meta,
        )


__all__ = ["DocumentIngester"]
