"""Abstract base class for document splitters.

A :class:`DocumentSplitter` accepts a :class:`LoadedDocument` and
returns a list of :class:`Chunk`s. The default implementation is
:class:`matrix.ingest.splitters.docling.DoclingSplitter`
(structure-aware, paired with :class:`DoclingLoader`);
:class:`matrix.ingest.splitters.recursive.RecursiveSplitter` is a
pure-Python fallback for callers that want no external parser.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from matrix.model.ingest import Chunk, LoadedDocument


class DocumentSplitter(ABC):
    """Split a :class:`LoadedDocument` into chunks.

    Implementations may use the ``LoadedDocument.structure`` field
    (if the loader populated it) for structure-aware splitting, or
    fall back to splitting the plain ``text``.
    """

    @abstractmethod
    def split(self, document: LoadedDocument) -> list[Chunk]:
        """Return chunks in document order.

        Synchronous because splitting is pure CPU work over text;
        callers that need to offload to a thread can do so themselves
        via :func:`asyncio.to_thread`.

        Implementations MUST emit at least one chunk for any non-empty
        document; an empty document produces an empty list.
        """


__all__ = ["DocumentSplitter"]
