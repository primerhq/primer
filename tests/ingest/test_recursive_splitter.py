"""Tests for matrix.ingest.splitters.recursive.RecursiveSplitter."""

from __future__ import annotations

import pytest

from primer.ingest.splitters.recursive import RecursiveSplitter
from primer.model.ingest import LoadedDocument


def _doc(text: str) -> LoadedDocument:
    return LoadedDocument(text=text)


class TestConstructor:
    def test_invalid_chunk_size(self) -> None:
        with pytest.raises(ValueError):
            RecursiveSplitter(chunk_size=0)

    def test_negative_overlap(self) -> None:
        with pytest.raises(ValueError):
            RecursiveSplitter(chunk_overlap=-1)

    def test_overlap_ge_chunk_size(self) -> None:
        with pytest.raises(ValueError):
            RecursiveSplitter(chunk_size=10, chunk_overlap=10)

    def test_empty_separators_rejected(self) -> None:
        with pytest.raises(ValueError):
            RecursiveSplitter(separators=[])


class TestSplit:
    def test_empty_input_returns_empty(self) -> None:
        s = RecursiveSplitter()
        assert s.split(_doc("")) == []

    def test_short_input_one_chunk(self) -> None:
        s = RecursiveSplitter(chunk_size=100, chunk_overlap=10)
        chunks = s.split(_doc("hello world"))
        assert len(chunks) == 1
        assert chunks[0].text == "hello world"
        assert chunks[0].position == 0

    def test_long_input_multiple_chunks(self) -> None:
        text = "\n\n".join(["x" * 80 for _ in range(5)])
        s = RecursiveSplitter(chunk_size=150, chunk_overlap=20)
        chunks = s.split(_doc(text))
        assert len(chunks) >= 3
        for i, c in enumerate(chunks):
            assert c.position == i

    def test_no_separators_hard_split(self) -> None:
        # No separators present in the text; falls back to character split.
        text = "x" * 300
        s = RecursiveSplitter(chunk_size=100, chunk_overlap=0)
        chunks = s.split(_doc(text))
        assert len(chunks) == 3
        for c in chunks:
            assert len(c.text) <= 100

    def test_position_zero_indexed_and_monotonic(self) -> None:
        text = "para one.\n\npara two.\n\npara three.\n\npara four.\n\npara five."
        s = RecursiveSplitter(chunk_size=20, chunk_overlap=0)
        chunks = s.split(_doc(text))
        positions = [c.position for c in chunks]
        assert positions == list(range(len(chunks)))

    def test_overlap_grows_chunks_after_first(self) -> None:
        # With overlap > 0, every chunk after the first carries the
        # tail of the prior piece prepended.
        text = "abcdefghij" * 30  # 300 chars, no separators
        s = RecursiveSplitter(chunk_size=50, chunk_overlap=10)
        chunks = s.split(_doc(text))
        assert len(chunks) >= 2
        for i in range(1, len(chunks)):
            assert len(chunks[i].text) >= 10
