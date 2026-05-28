"""Tests for matrix.model.ingest."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from primer.model.ingest import Chunk, IngestResult, LoadedDocument


# ---- LoadedDocument --------------------------------------------------------


class TestLoadedDocument:
    def test_construction_minimal(self) -> None:
        doc = LoadedDocument(text="hello world")
        assert doc.text == "hello world"
        assert doc.mime_type is None
        assert doc.structure is None
        assert doc.meta == {}

    def test_construction_full(self) -> None:
        doc = LoadedDocument(
            text="# Title\nbody",
            mime_type="text/markdown",
            structure={"kind": "doc", "blocks": []},
            meta={"page_count": 3},
        )
        assert doc.mime_type == "text/markdown"
        assert doc.structure == {"kind": "doc", "blocks": []}
        assert doc.meta["page_count"] == 3

    def test_round_trip(self) -> None:
        original = LoadedDocument(
            text="abc",
            mime_type="text/plain",
            structure={"x": 1},
            meta={"y": 2},
        )
        parsed = LoadedDocument.model_validate_json(original.model_dump_json())
        assert parsed == original


# ---- Chunk ----------------------------------------------------------------


class TestChunk:
    def test_construction(self) -> None:
        c = Chunk(text="foo", position=0)
        assert c.text == "foo"
        assert c.position == 0
        assert c.meta == {}

    def test_negative_position_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Chunk(text="x", position=-1)

    def test_round_trip(self) -> None:
        c = Chunk(text="bar", position=5, meta={"page": 2})
        parsed = Chunk.model_validate_json(c.model_dump_json())
        assert parsed == c


# ---- IngestResult ---------------------------------------------------------


class TestIngestResult:
    def test_construction(self) -> None:
        r = IngestResult(
            collection_id="kb-1",
            document_id="doc-1",
            chunks_indexed=5,
            dimensions=1536,
            replaced=False,
        )
        assert r.chunks_indexed == 5
        assert r.dimensions == 1536
        assert r.replaced is False
        assert r.bytes_loaded is None

    def test_zero_chunks_allowed(self) -> None:
        r = IngestResult(
            collection_id="kb-1",
            document_id="doc-1",
            chunks_indexed=0,
            dimensions=1,
            replaced=True,
        )
        assert r.chunks_indexed == 0
        assert r.replaced is True

    def test_zero_dimensions_rejected(self) -> None:
        with pytest.raises(ValidationError):
            IngestResult(
                collection_id="kb-1",
                document_id="doc-1",
                chunks_indexed=0,
                dimensions=0,
                replaced=False,
            )

    def test_empty_collection_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            IngestResult(
                collection_id="",
                document_id="doc-1",
                chunks_indexed=0,
                dimensions=1,
                replaced=False,
            )
