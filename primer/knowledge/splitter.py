"""Markdown heading-aware chunking for semantic indexing (spec section 6).

ATX headings outside fenced code blocks cut the document into sections;
each emitted chunk is prefixed with its heading breadcrumb so retrieval
keeps context, which is the granularity the old binary-document chunker
provided for the internal docs. Oversized sections and heading-free text
fall back to
paragraph packing (ported from the previous chunk_text), with an
optional character overlap between adjacent chunks of one section.
"""
from __future__ import annotations

import re

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE_RE = re.compile(r"^(```|~~~)")


def _pack_paragraphs(text: str, *, max_chars: int, overlap: int) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    hard_cap = max_chars * 2
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()] or [text]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        if len(para) > hard_cap:
            if current:
                chunks.append(current)
                current = ""
            for i in range(0, len(para), max_chars):
                chunks.append(para[i:i + max_chars])
            continue
        if not current:
            current = para
        elif len(current) + 2 + len(para) <= max_chars:
            current = f"{current}\n\n{para}"
        else:
            chunks.append(current)
            current = para
    if current:
        chunks.append(current)
    if overlap > 0 and len(chunks) > 1:
        overlapped = [chunks[0]]
        for prev, cur in zip(chunks, chunks[1:], strict=False):
            overlapped.append(prev[-overlap:] + cur)
        chunks = overlapped
    return chunks


def _sections(text: str) -> list[tuple[list[str], str]]:
    """Split on ATX headings outside fences -> [(breadcrumb_titles, body)]."""
    lines = text.splitlines()
    sections: list[tuple[list[str], list[str]]] = [([], [])]
    crumbs: list[tuple[int, str]] = []
    in_fence = False
    for line in lines:
        if _FENCE_RE.match(line.strip()):
            in_fence = not in_fence
        m = None if in_fence else _HEADING_RE.match(line)
        if m:
            level, title = len(m.group(1)), m.group(2).strip()
            crumbs = [(lv, t) for lv, t in crumbs if lv < level] + [(level, title)]
            sections.append(([t for _, t in crumbs], [line]))
        else:
            sections[-1][1].append(line)
    return [
        (titles, "\n".join(body).strip())
        for titles, body in sections
        if "\n".join(body).strip()
    ]


def split_text(text: str, *, max_chars: int = 1500, overlap: int = 200) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    parts = _sections(text)
    if len(parts) <= 1 and not (parts and parts[0][0]):
        return _pack_paragraphs(text, max_chars=max_chars, overlap=overlap)
    chunks: list[str] = []
    for titles, body in parts:
        crumb = "# " + " > ".join(titles) if titles else ""
        for piece in _pack_paragraphs(body, max_chars=max_chars, overlap=overlap):
            if crumb and not piece.startswith(crumb):
                chunks.append(f"{crumb}\n{piece}")
            else:
                chunks.append(piece)
    return chunks


__all__ = ["split_text"]
