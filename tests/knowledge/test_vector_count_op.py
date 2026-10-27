"""RED probes for the VectorStore ``count_documents`` op (G10-1, audit /var/tmp/revamp/audit/G10.md).

``search_status`` counts the indexed documents with
``store.search_by_meta(collection_id, meta={})`` and ``len({r.document_id})``.
Both shipped backends implement ``search_by_meta`` as a full-namespace scan
returning COMPLETE records (pgvector selects ``text, vector, meta`` with no
LIMIT; lance runs ``table.query().to_list()`` with no projection), so every
``GET /collections/{id}/search`` and every 1-5 s ``bootstrap/status`` poll
materializes the whole namespace just to render ``N/M docs``.

These probes pin the fix contract and MUST FAIL against the pre-fix code
(commit 1 of the red-first flow):

* (a) ``search_status`` gets its count from ``count_documents`` and never
  calls ``search_by_meta``.
* (b) the ``VectorStore`` ABC declares ``count_documents`` (abstract).
* (c) the pgvector count path is a ``count(DISTINCT document_id)``-style
  query: no LIMIT, no vector column (static source probe, house pattern).
* (e) ``count_documents`` raising keeps ``search_status`` best-effort:
  the state is still returned with ``documents_indexed == 0``.
* (d) the lance count is a projected scan: ``document_id`` only, no
  vectors / text / meta pulled across the wire.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from primer.int.vector_store import VectorStore
from primer.knowledge.lifecycle import search_status
from primer.model.collection import (
    Collection,
    CollectionEmbedder,
    CollectionSearchConfig,
    Document,
)

ROOT = Path(__file__).resolve().parents[2]


# ---- fakes ---------------------------------------------------------------


class _CountingStore(VectorStore):
    """ABC-subclassing fake: ``count_documents`` is real, ``search_by_meta``
    records the call (and may raise), so a fix that still calls it is
    caught by the call count, not just by the masked zero."""

    def __init__(
        self,
        count: int | None = None,
        count_exc: Exception | None = None,
        meta_records: list[Any] | None = None,
        meta_exc: Exception | None = None,
    ) -> None:
        self._count = count
        self._count_exc = count_exc
        self._meta_records = list(meta_records or [])
        self._meta_exc = meta_exc
        self.count_calls = 0
        self.meta_calls = 0

    async def count_documents(self, collection_id: str) -> int:
        self.count_calls += 1
        if self._count_exc is not None:
            raise self._count_exc
        return self._count

    async def search_by_meta(
        self, collection_id: str, meta: dict[str, Any]
    ) -> list[Any]:
        self.meta_calls += 1
        if self._meta_exc is not None:
            raise self._meta_exc
        return list(self._meta_records)

    # Abstract members the probe does not exercise.
    async def create_collection(self, collection_id, *, dimensions, distance="cosine"):
        raise NotImplementedError

    async def put(self, record):
        raise NotImplementedError

    async def search(self, collection_id, vector, k):
        raise NotImplementedError

    async def get(self, collection_id, document_id):
        raise NotImplementedError

    async def delete(self, collection_id, document_id):
        raise NotImplementedError

    async def drop_collection(self, collection_id):
        raise NotImplementedError


class _Page:
    def __init__(self, items: list[Any]) -> None:
        self.items = items


class _CollectionStorage:
    def __init__(self, row: Collection | None) -> None:
        self._row = row

    async def get(self, entity_id: str):
        return self._row


class _DocumentStorage:
    def __init__(self, docs: list[Document]) -> None:
        self._docs = docs

    async def find(self, predicate, page):
        # One short page: _collection_documents stops paginating.
        return _Page(list(self._docs))


class _StorageProvider:
    def __init__(self, row: Collection | None, docs: list[Document]) -> None:
        self._row = row
        self._docs = docs

    def get_storage(self, model):
        if model is Collection:
            return _CollectionStorage(self._row)
        if model is Document:
            return _DocumentStorage(self._docs)
        raise AssertionError(f"unexpected storage request: {model!r}")


class _Registry:
    def __init__(self, store: _CountingStore) -> None:
        self._store = store

    async def get_store(self, ssp_id: str) -> _CountingStore:
        assert ssp_id == "ssp-x"
        return self._store


def _collection() -> Collection:
    return Collection(
        id="kb",
        description="kb wiki",
        search=CollectionSearchConfig(
            embedder=CollectionEmbedder(provider_id="emb", model="text-1"),
            vector_store_provider_id="ssp-x",
            state="ready",
        ),
    )


def _docs(n: int) -> list[Document]:
    return [Document(id=f"doc-{i}", collection_id="kb", slug=str(i), path=f"p/{i}") for i in range(n)]


def _meta_record(document_id: str):
    class _R:
        def __init__(self, document_id: str) -> None:
            self.document_id = document_id

    return _R(document_id)


# (a) search_status counts via count_documents, never via search_by_meta


async def test_search_status_counts_via_count_documents() -> None:
    store = _CountingStore(
        count=42, meta_exc=RuntimeError("search_by_meta must not be called")
    )
    status = await search_status(
        _StorageProvider(_collection(), _docs(5)), _Registry(store),
        collection_id="kb",
    )
    assert status.state == "ready"
    assert status.documents_total == 5
    assert status.documents_indexed == 42, (
        "search_status must take documents_indexed from the cheap count op; "
        f"got {status.documents_indexed} (count_calls={store.count_calls}, "
        f"meta_calls={store.meta_calls})"
    )
    assert store.count_calls == 1
    assert store.meta_calls == 0, (
        "search_status must not full-scan the namespace to count documents"
    )


# (b) the ABC declares the op


def test_vector_store_abc_declares_count_documents() -> None:
    assert hasattr(VectorStore, "count_documents"), (
        "VectorStore must declare count_documents (a cheap distinct-document "
        "count with no record materialization)"
    )
    assert "count_documents" in getattr(VectorStore, "__abstractmethods__", set()), (
        "count_documents must be abstract: every backend must decide its own "
        "cheap count, so no default may materialize records"
    )


# (c) the pgvector count path is a distinct-document count, not a fetch


def _count_sql_source() -> str:
    src = (ROOT / "primer/vector/pgvector.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == "count_documents":
            body = list(node.body)
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                body = body[1:]  # skip the docstring
            chunks: list[str] = []
            for sub in ast.walk(ast.Module(body=body, type_ignores=[])):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    chunks.append(sub.value)
                elif isinstance(sub, ast.JoinedStr):
                    chunks.extend(
                        part.value for part in sub.values
                        if isinstance(part, ast.Constant) and isinstance(part.value, str)
                    )
            return " ".join(chunks)
    pytest.fail("primer/vector/pgvector.py has no count_documents method")


def test_pgvector_count_is_a_distinct_document_count() -> None:
    sql = _count_sql_source().lower()
    norm = " ".join(sql.split())
    assert "count(distinct document_id)" in norm, (
        f"the pgvector count path must count DISTINCT document_id; got: {norm!r}"
    )
    assert "limit" not in norm, "the count query must not be capped"
    assert "vector" not in norm, (
        "the count path must not select or decode the vector column"
    )


# (e) a failing count stays best-effort


async def test_search_status_survives_a_failing_count() -> None:
    store = _CountingStore(
        count_exc=RuntimeError("store unreachable"),
        meta_records=[_meta_record(f"d{i}") for i in range(3)],
    )
    status = await search_status(
        _StorageProvider(_collection(), _docs(5)), _Registry(store),
        collection_id="kb",
    )
    assert status.state == "ready"
    assert status.documents_total == 5
    assert status.documents_indexed == 0, (
        "an unreachable store must read as zero indexed documents, not as "
        f"the full-scan result ({status.documents_indexed})"
    )
    assert store.count_calls == 1
    assert store.meta_calls == 0


# (d) the lance count is a projected scan


class _LanceQuery:
    def __init__(self) -> None:
        self.selected: list[str] | None = None
        self._rows = [
            {"document_id": "d0"},
            {"document_id": "d1"},
            {"document_id": "d1"},
        ]

    def select(self, columns: list[str]) -> "_LanceQuery":
        self.selected = list(columns)
        return self

    async def to_list(self) -> list[dict]:
        # A store that requested more than document_id would pull the
        # full rows (vectors included): fail the probe on the projection.
        if self.selected != ["document_id"]:
            raise AssertionError(f"unexpected projection: {self.selected!r}")
        return [dict(r) for r in self._rows]


class _LanceTable:
    def __init__(self) -> None:
        self._query = _LanceQuery()

    def query(self) -> _LanceQuery:
        return self._query


async def test_lance_count_is_a_projected_document_id_scan() -> None:
    pytest.importorskip("lancedb")  # house pattern: soft-skip without the extra
    from primer.vector.lance import LanceVectorStore

    assert hasattr(LanceVectorStore, "count_documents"), (
        "LanceVectorStore must implement the cheap count op"
    )
    store = LanceVectorStore.__new__(LanceVectorStore)  # type: ignore[abstract]
    table = _LanceTable()

    async def _open_table(collection_id: str) -> _LanceTable:
        assert collection_id == "kb"
        return table

    store._open_table = _open_table  # type: ignore[method-assign]
    assert await store.count_documents("kb") == 2
    assert table._query.selected == ["document_id"]
