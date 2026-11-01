"""Heading-aware splitter replacing the old binary-document chunking."""
from __future__ import annotations

import pytest

from primer.knowledge.splitter import split_text


MD = """# Guide

intro paragraph

## Install

install text

## Usage

usage text
"""


def test_headings_cut_sections_with_breadcrumbs():
    chunks = split_text(MD, max_chars=60, overlap=0)
    assert any(c.startswith("# Guide > Install") for c in chunks)
    assert any(c.startswith("# Guide > Usage") for c in chunks)


def test_small_doc_is_one_chunk():
    chunks = split_text(MD, max_chars=5000, overlap=0)
    assert len(chunks) == 1 and chunks[0].startswith("# Guide")


def test_fenced_hash_is_not_a_heading():
    md = "# T\n\n```\n# not a heading\n```\n\ntail"
    chunks = split_text(md, max_chars=20, overlap=0)
    assert not any(c.startswith("# T > not a heading") for c in chunks)


def test_plain_text_falls_back_to_paragraph_packing():
    text = "\n\n".join(f"para {i} " + "x" * 40 for i in range(10))
    chunks = split_text(text, max_chars=120, overlap=0)
    assert len(chunks) > 1
    assert all(len(c) <= 240 for c in chunks)


def test_overlap_carries_tail():
    text = "\n\n".join("y" * 100 for _ in range(6))
    chunks = split_text(text, max_chars=150, overlap=30)
    assert chunks[1][:30] == chunks[0][-30:]


def test_empty_input():
    assert split_text("") == []


# ---- KN-19i: fences close only on their own kind; CRLF paragraphs split like LF ones -------------------------------------------------


def _lf_doc() -> str:
    parts = ["# Guide", "", "intro " * 40, "", "## Install", "", "```bash", "# a comment, not a heading", "pip install x", "```", "",
             "install " * 60, "", "~~~", "# tilde comment", "~~~", "", "### Deep", "",
             "\n\n".join(f"para {i} " + "y" * 90 for i in range(12)), "", "## Usage", "", "usage " * 300]
    return "\n".join(parts)


_LF_PLAIN = "\n\n".join(f"plain {i} " + "z" * 70 for i in range(30))


@pytest.mark.parametrize(("name", "max_chars", "overlap", "count", "digest"), [
    ("md", 1500, 200, 5, "cf59964cbf1a30df"),
    ("md", 300, 0, 16, "770a4af9673e11e8"),
    ("md", 200, 40, 24, "92e341a6ed92fb2c"),
    ("plain", 1500, 200, 2, "e80d11cdb08d01ce"),
    ("plain", 300, 0, 10, "9ece500495e6907d"),
    ("plain", 200, 40, 15, "ee944acb4566e475"),
])
def test_lf_documents_without_nested_fences_chunk_exactly_as_before(name, max_chars, overlap, count, digest):
    """Characterization: the fence and CRLF fixes leave LF chunk boundaries alone, so nothing re-indexes."""
    import hashlib
    import json

    out = split_text(_lf_doc() if name == "md" else _LF_PLAIN, max_chars=max_chars, overlap=overlap)
    assert (len(out), hashlib.sha256(json.dumps(out).encode()).hexdigest()[:16]) == (count, digest)


def test_a_tilde_line_inside_a_backtick_fence_does_not_close_it():
    md = "# T\n\n```\n~~~\n# not a heading\n```\n\n" + "tail " * 20
    chunks = split_text(md, max_chars=20, overlap=0)
    assert not any("not a heading" in c.split("\n", 1)[0] for c in chunks)


def test_a_shorter_fence_inside_a_longer_one_does_not_close_it():
    md = "# T\n\n````\n```\n# not a heading\n```\n````\n\n" + "tail " * 20
    chunks = split_text(md, max_chars=20, overlap=0)
    assert not any("not a heading" in c.split("\n", 1)[0] for c in chunks)


def test_crlf_paragraphs_split_like_lf_ones():
    assert split_text(_LF_PLAIN.replace("\n", "\r\n"), max_chars=300, overlap=0) == split_text(_LF_PLAIN, max_chars=300, overlap=0)
