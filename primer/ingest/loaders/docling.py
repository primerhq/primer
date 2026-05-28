"""Docling-backed :class:`DocumentLoader`.

The default document loader used throughout :mod:`primer.ingest`.
Wraps :class:`docling.document_converter.DocumentConverter` to turn
PDFs / DOCX / PPTX / HTML / etc. into a :class:`LoadedDocument`. The
serialised :class:`DoclingDocument` is stored under
``LoadedDocument.structure`` so a downstream
:class:`primer.ingest.splitters.docling.DoclingSplitter` can use it
without re-parsing.

``docling`` ships as a core dependency, so this loader is always
available and is what callers should construct unless they have a
specific reason to substitute another :class:`DocumentLoader`.
"""

from __future__ import annotations

import asyncio
import logging
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

from docling.datamodel.base_models import DocumentStream
from docling.document_converter import DocumentConverter

from primer.ingest.loader import DocumentLoader
from primer.model.except_ import (
    BadRequestError,
    UnsupportedContentError,
)
from primer.model.ingest import LoadedDocument


logger = logging.getLogger(__name__)


class DoclingLoader(DocumentLoader):
    """High-quality multi-format loader backed by Docling."""

    def __init__(self) -> None:
        self._converter = DocumentConverter()

    async def load(self, source: bytes | Path | str) -> LoadedDocument:
        resolved = self._resolve_source(source)
        return await asyncio.to_thread(self._load_blocking, resolved)

    # ---- Internals -------------------------------------------------------

    def _resolve_source(
        self,
        source: bytes | Path | str,
    ) -> bytes | Path | str:
        """Return the raw input docling will accept (bytes/Path/URL string)."""
        if isinstance(source, bytes):
            return source
        if isinstance(source, Path):
            if not source.exists():
                raise BadRequestError(
                    f"DoclingLoader: source path does not exist: {source!r}"
                )
            return source
        if isinstance(source, str):
            parsed = urlparse(source)
            if parsed.scheme in ("http", "https"):
                return source
            path = Path(source)
            if path.exists():
                return path
            raise BadRequestError(
                f"DoclingLoader: source string is neither an existing path "
                f"nor a URL: {source!r}"
            )
        raise BadRequestError(
            f"DoclingLoader: unsupported source type {type(source).__name__}"
        )

    def _load_blocking(
        self,
        source: bytes | Path | str,
    ) -> LoadedDocument:
        if isinstance(source, bytes):
            stream = DocumentStream(name="bytes-source", stream=BytesIO(source))
            try:
                result = self._converter.convert(stream)
            except Exception as exc:
                raise UnsupportedContentError(
                    f"DoclingLoader: failed to parse bytes source: {exc}"
                ) from exc
            bytes_loaded: int | None = len(source)
        else:
            try:
                result = self._converter.convert(source)
            except Exception as exc:
                raise UnsupportedContentError(
                    f"DoclingLoader: failed to parse source {source!r}: {exc}"
                ) from exc
            bytes_loaded = (
                source.stat().st_size if isinstance(source, Path) else None
            )

        try:
            text = result.document.export_to_markdown()
        except Exception as exc:  # pragma: no cover -- docling internal failure
            raise UnsupportedContentError(
                f"DoclingLoader: failed to export markdown: {exc}"
            ) from exc

        try:
            structure = result.document.export_to_dict()
        except Exception:  # pragma: no cover -- structure is optional
            structure = None

        meta: dict = {}
        if bytes_loaded is not None:
            meta["bytes_loaded"] = bytes_loaded
        if isinstance(source, str) and urlparse(source).scheme in ("http", "https"):
            meta["source_url"] = source
        if isinstance(source, Path):
            meta["source_path"] = str(source)

        mime_type: str | None = None
        try:
            mime_type = result.input.format.value if result.input else None
        except Exception:  # pragma: no cover -- attribute layout varies
            mime_type = None

        return LoadedDocument(
            text=text,
            mime_type=mime_type,
            structure=structure,
            meta=meta,
        )


__all__ = ["DoclingLoader"]
