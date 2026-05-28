"""Abstract base class for document loaders.

A :class:`DocumentLoader` accepts a source (raw bytes, filesystem
path, or string that resolves to a path or URL) and produces a
:class:`LoadedDocument`. The default implementation is
:class:`primer.ingest.loaders.docling.DoclingLoader` (re-exported
as :class:`primer.ingest.DoclingLoader`); subclasses can substitute
a custom backend by implementing :meth:`load`.

Implementations bind to a backend library at construction time;
construction is cheap; the heavy work happens in :meth:`load`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from primer.model.ingest import LoadedDocument


class DocumentLoader(ABC):
    """Read one document from a source and emit a :class:`LoadedDocument`.

    Subclasses bind to a backend library (Docling, MarkItDown, etc.)
    at construction time. Construction is cheap; the heavy work
    happens in :meth:`load`.
    """

    @abstractmethod
    async def load(self, source: bytes | Path | str) -> LoadedDocument:
        """Read ``source`` and return its parsed content.

        ``source`` is one of:

        * :class:`bytes` -- raw document bytes; the loader sniffs
          mime type from the source's bytes.
        * :class:`Path` -- filesystem path to read.
        * :class:`str` -- either a filesystem path (if it exists) or
          an HTTP(S) URL (if it parses as one). Loaders that don't
          fetch URLs raise
          :class:`primer.model.except_.UnsupportedContentError`.

        Implementations that can't handle the resolved mime type raise
        :class:`UnsupportedContentError` rather than silently emitting
        empty text.

        Implementations SHOULD run any blocking I/O via
        :func:`asyncio.to_thread` so the surrounding event loop is
        not blocked.

        Raises
        ------
        primer.model.except_.BadRequestError
            ``source`` is a string that resolves to neither a path
            nor a URL.
        primer.model.except_.UnsupportedContentError
            The loader can't handle the resolved mime type, or the
            ``source`` is a URL and this loader doesn't fetch URLs.
        primer.model.except_.ConfigError
            The loader's backend library is not installed.
        """


__all__ = ["DocumentLoader"]
