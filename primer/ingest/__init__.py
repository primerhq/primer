"""Document ingestion -- load, split, embed, store.

Public surface:

* :class:`DocumentLoader` -- ABC for reading a document from a source.
* :class:`DocumentSplitter` -- ABC for splitting a loaded document
  into chunks.
* :class:`DocumentIngester` -- concrete orchestrator that drives
  ``loader.load -> splitter.split -> embedder.embed -> vector_store.put``
  for one :class:`primer.model.collection.Document`.
* :class:`DoclingLoader` -- DEFAULT loader. Wraps Docling for
  high-quality multi-format parsing (PDF / DOCX / PPTX / HTML /
  ...). Requires the optional ``docling`` extra; imported lazily via
  :pep:`562` ``__getattr__`` so importing this package never pulls the
  heavy ingestion / OCR stack.
* :class:`DoclingSplitter` -- DEFAULT splitter. Structure-aware
  chunking via Docling's :class:`HybridChunker`; consumes the
  structural metadata that :class:`DoclingLoader` populates. Also part
  of the ``docling`` extra and imported lazily.
* :class:`RecursiveSplitter` -- pure-Python recursive splitter; a
  zero-dep fallback for callers that don't want the structural
  metadata path.

The :class:`DocumentIngester` constructor defaults its ``loader`` and
``splitter`` to fresh :class:`DoclingLoader` / :class:`DoclingSplitter`
instances when omitted, so document ingestion needs the ``docling``
extra unless a custom backend is supplied.

See ``docs/superpowers/specs/2026-05-03-document-ingestion-design.md``.
"""

from typing import TYPE_CHECKING

from primer.ingest.ingester import DocumentIngester
from primer.ingest.loader import DocumentLoader
from primer.ingest.splitter import DocumentSplitter
from primer.ingest.splitters.recursive import RecursiveSplitter
from primer.model.ingest import Chunk, IngestResult, LoadedDocument

if TYPE_CHECKING:
    from primer.ingest.loaders.docling import DoclingLoader
    from primer.ingest.splitters.docling import DoclingSplitter


__all__ = [
    "Chunk",
    "DoclingLoader",
    "DoclingSplitter",
    "DocumentIngester",
    "DocumentLoader",
    "DocumentSplitter",
    "IngestResult",
    "LoadedDocument",
    "RecursiveSplitter",
]


def __getattr__(name: str):
    # DoclingLoader / DoclingSplitter need the optional 'docling' extra.
    if name in ("DoclingLoader", "DoclingSplitter"):
        try:
            if name == "DoclingLoader":
                from primer.ingest.loaders.docling import DoclingLoader as obj
            else:
                from primer.ingest.splitters.docling import (
                    DoclingSplitter as obj,
                )
        except ModuleNotFoundError as exc:  # pragma: no cover - import guard
            raise ModuleNotFoundError(
                f"{name} requires the optional 'docling' extra. Install it "
                "with: pip install 'primer-ai[docling]' (or "
                "'primer-ai[full]' for everything)."
            ) from exc
        return obj
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
