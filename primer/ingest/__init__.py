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
  ...). Always available because ``docling`` is a core dependency.
* :class:`DoclingSplitter` -- DEFAULT splitter. Structure-aware
  chunking via Docling's :class:`HybridChunker`; consumes the
  structural metadata that :class:`DoclingLoader` populates.
* :class:`RecursiveSplitter` -- pure-Python recursive splitter; a
  zero-dep fallback for callers that don't want the structural
  metadata path.

The :class:`DocumentIngester` constructor defaults its ``loader`` and
``splitter`` to fresh :class:`DoclingLoader` / :class:`DoclingSplitter`
instances when omitted; callers only need to supply them when
substituting a custom backend.

See ``docs/superpowers/specs/2026-05-03-document-ingestion-design.md``.
"""

from primer.ingest.ingester import DocumentIngester
from primer.ingest.loader import DocumentLoader
from primer.ingest.loaders.docling import DoclingLoader
from primer.ingest.splitter import DocumentSplitter
from primer.ingest.splitters.docling import DoclingSplitter
from primer.ingest.splitters.recursive import RecursiveSplitter
from primer.model.ingest import Chunk, IngestResult, LoadedDocument


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
