"""Docling HybridChunker-backed :class:`DocumentSplitter`.

The default structure-aware splitter used throughout
:mod:`matrix.ingest`. Wraps :class:`docling.chunking.HybridChunker`
to produce structure-aware chunks from a :class:`LoadedDocument`
whose ``structure`` field carries a serialised
:class:`DoclingDocument` (populated by
:class:`matrix.ingest.loaders.docling.DoclingLoader`).

``docling`` is a core dependency, so this splitter is always
available alongside :class:`DoclingLoader`.
"""

from __future__ import annotations

import logging

from docling.chunking import HybridChunker
from docling_core.types.doc.document import DoclingDocument

from matrix.ingest.splitter import DocumentSplitter
from matrix.model.except_ import ConfigError
from matrix.model.ingest import Chunk, LoadedDocument


logger = logging.getLogger(__name__)


class DoclingSplitter(DocumentSplitter):
    """Structure-aware splitter backed by Docling's HybridChunker."""

    def __init__(self) -> None:
        self._chunker = HybridChunker()

    def split(self, document: LoadedDocument) -> list[Chunk]:
        if document.structure is None:
            raise ConfigError(
                "DoclingSplitter requires a LoadedDocument produced by "
                "DoclingLoader (structure is None)"
            )
        try:
            doc = DoclingDocument.model_validate(document.structure)
        except Exception as exc:
            raise ConfigError(
                f"DoclingSplitter: failed to parse structure as "
                f"DoclingDocument: {exc}"
            ) from exc

        chunks: list[Chunk] = []
        for i, raw in enumerate(self._chunker.chunk(doc)):
            text = getattr(raw, "text", None) or str(raw)
            meta: dict = {}
            heading_path = getattr(raw, "headings", None)
            if heading_path:
                meta["heading_path"] = list(heading_path)
            page = getattr(raw, "page", None)
            if page is not None:
                meta["page"] = page
            chunks.append(Chunk(text=text, position=i, meta=meta))
        return chunks


__all__ = ["DoclingSplitter"]
