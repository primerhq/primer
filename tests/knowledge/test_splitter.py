"""Heading-aware splitter replacing the old binary-document chunking."""
from __future__ import annotations

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
