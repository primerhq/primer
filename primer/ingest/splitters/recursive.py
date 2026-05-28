"""Pure-Python recursive splitter.

Splits text by trying a hierarchy of separators until each piece
fits within ``chunk_size`` characters; re-joins adjacent pieces with
``chunk_overlap`` characters of context to preserve continuity.

No third-party dependencies. Mirrors the algorithm of langchain's
``RecursiveCharacterTextSplitter`` (which placed first in the
February 2026 academic-paper chunking benchmark; see
``research/`` for the survey notes).
"""

from __future__ import annotations

from collections.abc import Sequence

from primer.ingest.splitter import DocumentSplitter
from primer.model.ingest import Chunk, LoadedDocument


_DEFAULT_SEPARATORS: tuple[str, ...] = ("\n\n", "\n", ". ", " ", "")


class RecursiveSplitter(DocumentSplitter):
    """Recursive character-based splitter.

    Algorithm:

    1. If the input text fits in one chunk, return one chunk.
    2. Otherwise, walk the separator hierarchy from largest to
       smallest. For each separator, split the text on it and group
       adjacent pieces into chunks of up to ``chunk_size`` characters.
       If any individual piece is still over ``chunk_size``, recurse
       into it with the next separator down.
    3. Re-glue adjacent chunks with ``chunk_overlap`` characters of
       trailing context so search hits don't lose semantic continuity
       at chunk boundaries.

    Chunks are emitted in document order, 0-indexed.
    """

    def __init__(
        self,
        *,
        chunk_size: int = 512,
        chunk_overlap: int = 64,
        separators: Sequence[str] = _DEFAULT_SEPARATORS,
    ) -> None:
        if chunk_size <= 0:
            raise ValueError(f"chunk_size must be > 0, got {chunk_size!r}")
        if chunk_overlap < 0:
            raise ValueError(f"chunk_overlap must be >= 0, got {chunk_overlap!r}")
        if chunk_overlap >= chunk_size:
            raise ValueError(
                f"chunk_overlap must be < chunk_size; got {chunk_overlap!r} >= {chunk_size!r}"
            )
        if not separators:
            raise ValueError("separators must contain at least one entry")
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._separators = tuple(separators)

    # ---- Public surface --------------------------------------------------

    def split(self, document: LoadedDocument) -> list[Chunk]:
        text = document.text
        if not text:
            return []
        pieces = self._split_text(text, list(self._separators))
        chunks_with_overlap = self._merge_with_overlap(pieces)
        return [
            Chunk(text=t, position=i, meta={})
            for i, t in enumerate(chunks_with_overlap)
        ]

    # ---- Internals -------------------------------------------------------

    def _split_text(self, text: str, separators: list[str]) -> list[str]:
        """Recursive split. Returns a list of pieces each <= chunk_size."""
        if len(text) <= self._chunk_size:
            return [text]
        if not separators:
            return [
                text[i : i + self._chunk_size]
                for i in range(0, len(text), self._chunk_size)
            ]

        sep, *rest = separators
        if sep == "":
            return [
                text[i : i + self._chunk_size]
                for i in range(0, len(text), self._chunk_size)
            ]

        parts = text.split(sep)
        out: list[str] = []
        for part in parts:
            if len(part) <= self._chunk_size:
                out.append(part)
            else:
                out.extend(self._split_text(part, rest))

        return self._glue(out, sep)

    def _glue(self, parts: list[str], sep: str) -> list[str]:
        """Greedily re-join adjacent parts up to ``chunk_size`` characters."""
        out: list[str] = []
        cur = ""
        for part in parts:
            candidate = cur + sep + part if cur else part
            if len(candidate) <= self._chunk_size:
                cur = candidate
            else:
                if cur:
                    out.append(cur)
                cur = part
        if cur:
            out.append(cur)
        return out

    def _merge_with_overlap(self, pieces: list[str]) -> list[str]:
        """Add ``chunk_overlap`` chars of trailing context between pieces."""
        if self._chunk_overlap == 0 or len(pieces) <= 1:
            return pieces
        out: list[str] = [pieces[0]]
        for i in range(1, len(pieces)):
            prev = pieces[i - 1]
            tail = (
                prev[-self._chunk_overlap :]
                if self._chunk_overlap < len(prev)
                else prev
            )
            out.append(tail + pieces[i])
        return out


__all__ = ["RecursiveSplitter"]
