"""VectorStore.replace_document (review 2026-11-23 X2.3, the M3 residual): a shrinking re-index must
not leave a window in which the document is unsearchable.

The default implementation keeps the 3.11 ordering (the destructive step only when the new set is
shorter than the stored one; a blipped put is retried a bounded number of times while the vectors
are in hand) and is NOT atomic. pgvector overrides it with one transaction (delete + upsert), so a
failed replace rolls back to the old chunks; LanceDB has no cross-operation transaction, so its
override is delete-then-merge with the same ordering.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from urllib.parse import urlparse

import asyncpg
import pytest
import pytest_asyncio

from primer.int.vector_store import VectorStore
from primer.model.except_ import BadRequestError, PrimerError
from primer.model.provider import LanceConfig, PgVectorConfig
from primer.model.vector import EmbeddingRecord, SearchResult, Vector
from primer.vector.lance import LanceVectorStoreProvider
from primer.vector.pgvector import PgVectorStoreProvider
from tests.pg_gate import explicit_port, needs_postgres, require_postgres_url

pg = needs_postgres("the replace_document tests")


def _rec(document_id: str, chunk_id: str, *, n: int = 4, text: str = "t",
         collection_id: str = "c") -> EmbeddingRecord:
    return EmbeddingRecord(
        collection_id=collection_id,
        document_id=document_id,
        chunk_id=chunk_id,
        text=text,
        vector=[0.1] * n,
        meta={},
    )


# ---------- the default implementation (the house ordering, non-atomic) ----------


class _MemStore(VectorStore):
    """An in-memory VectorStore: the default replace_document runs on it for real, only the writes
    are faked."""

    def __init__(self) -> None:
        self.rows: dict[tuple, EmbeddingRecord] = {}
        self.deletes: list[tuple[str, str]] = []
        self.put_many_calls: list[list[EmbeddingRecord]] = []
        self.fail_put_many: int = 0  # the next N put_many calls raise, then succeed

    async def create_collection(self, collection_id: str, dimensions: int,
                                distance: str = "cosine") -> None:
        pass

    async def put(self, record: EmbeddingRecord) -> None:
        self.rows[(record.collection_id, record.document_id, record.chunk_id)] = record

    async def put_many(self, records: list[EmbeddingRecord]) -> None:
        self.put_many_calls.append(list(records))
        if self.fail_put_many > 0:
            self.fail_put_many -= 1
            raise PrimerError("store blip")
        for record in records:
            await self.put(record)

    async def search(self, collection_id: str, vector: Vector, k: int) -> list[SearchResult]:
        raise NotImplementedError

    async def search_by_meta(self, collection_id: str,
                             meta: Mapping) -> list[EmbeddingRecord]:
        return [r for r in self.rows.values() if r.collection_id == collection_id]

    async def count_documents(self, collection_id: str) -> int:
        return len({r.document_id for r in self.rows.values() if r.collection_id == collection_id})

    async def get(self, collection_id: str, document_id: str) -> list[EmbeddingRecord]:
        return [r for k, r in sorted(self.rows.items())
                if k[0] == collection_id and k[1] == document_id]

    async def delete(self, collection_id: str, document_id: str) -> None:
        self.deletes.append((collection_id, document_id))
        for key in [k for k in self.rows if k[0] == collection_id and k[1] == document_id]:
            del self.rows[key]

    async def drop_collection(self, collection_id: str) -> None:
        for key in [k for k in self.rows if k[0] == collection_id]:
            del self.rows[key]


@pytest.mark.asyncio
async def test_the_default_replaces_a_shrunk_document_with_one_delete():
    """Old: 3 chunks, new: 1. The higher-indexed chunks must go (the delete runs once), and the new
    set lands."""
    s = _MemStore()
    for i in range(3):
        await s.put(_rec("d1", str(i)))
    await s.replace_document("c", "d1", [_rec("d1", "0", text="new")])
    assert s.deletes == [("c", "d1")]
    out = await s.get("c", "d1")
    assert [r.chunk_id for r in out] == ["0"] and out[0].text == "new"


@pytest.mark.asyncio
async def test_the_default_replaces_a_grown_document_without_a_delete():
    """Old: 1 chunk, new: 3. A new set at least as long as the stored one overwrites every stored
    chunk id: no destructive step runs, and none may."""
    s = _MemStore()
    await s.put(_rec("d1", "0"))
    await s.replace_document("c", "d1", [_rec("d1", str(i)) for i in range(3)])
    assert s.deletes == []
    assert [r.chunk_id for r in await s.get("c", "d1")] == ["0", "1", "2"]


@pytest.mark.asyncio
async def test_the_default_retries_the_put_while_the_vectors_are_in_hand():
    s = _MemStore()
    for i in range(2):
        await s.put(_rec("d1", str(i)))
    s.fail_put_many = 1  # the first put_many of the replace blips
    await s.replace_document("c", "d1", [_rec("d1", "0")])
    assert s.deletes == [("c", "d1")]  # the destructive step ran exactly once
    assert len(s.put_many_calls) == 2  # and the retry landed
    assert [r.chunk_id for r in await s.get("c", "d1")] == ["0"]


@pytest.mark.asyncio
async def test_the_default_gives_up_after_three_blipped_puts():
    s = _MemStore()
    for i in range(2):  # a real shrink, so the destructive step runs
        await s.put(_rec("d1", str(i)))
    s.fail_put_many = 99
    with pytest.raises(PrimerError):
        await s.replace_document("c", "d1", [_rec("d1", "0")])
    assert len(s.put_many_calls) == 3  # a bounded number of attempts, no fourth
    assert s.deletes == [("c", "d1")]


@pytest.mark.asyncio
async def test_the_default_replacing_with_nothing_deletes_only_when_something_is_stored():
    s = _MemStore()
    await s.put(_rec("d1", "0"))
    await s.replace_document("c", "d1", [])
    assert s.deletes == [("c", "d1")] and await s.get("c", "d1") == []
    await s.replace_document("c", "d-never-stored", [])  # idempotent, no delete, no error
    assert s.deletes == [("c", "d1")]


# ---------- pgvector: one transaction, a failed replace rolls back ----------


def _conn_args() -> dict:
    u = urlparse(require_postgres_url("the replace_document tests"))
    return {
        "host": u.hostname or "localhost",
        "port": explicit_port(u),
        "user": u.username or "postgres",
        "password": u.password or "",
        "database": (u.path or "/postgres").lstrip("/") or "postgres",
    }


async def _provider(schema: str) -> PgVectorStoreProvider:
    a = _conn_args()
    p = PgVectorStoreProvider(PgVectorConfig(
        hostname=a["host"], port=a["port"], username=a["user"],
        password=a["password"], database=a["database"], db_schema=schema,
    ))
    await p.initialize()
    return p


async def _drop_schema(schema: str) -> None:
    conn = await asyncpg.connect(**_conn_args())
    try:
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    finally:
        await conn.close()


class _BlippingPool:
    """A proxy over the real pool: the FIRST executemany raises, everything else delegates.

    The replace's DELETE then runs for real (a real statement in a real transaction on a real
    connection) and the upsert's executemany is the point of failure: a transactional replace must
    roll the delete back, so the old chunks stay searchable.
    """

    def __init__(self, real) -> None:
        self._real = real
        self.blipped = False

    def acquire(self):
        inner = self._real.acquire()
        pool = self

        class _CM:
            async def __aenter__(self):
                self.conn = _BlippingConn(await inner.__aenter__(), pool)
                return self.conn

            async def __aexit__(self, *exc):
                return await inner.__aexit__(*exc)

        return _CM()


class _BlippingConn:
    def __init__(self, conn, pool: _BlippingPool) -> None:
        self._conn = conn
        self._pool = pool

    def transaction(self):
        return self._conn.transaction()

    async def execute(self, *args, **kwargs):
        return await self._conn.execute(*args, **kwargs)

    async def executemany(self, *args, **kwargs):
        if not self._pool.blipped:
            self._pool.blipped = True
            raise asyncpg.exceptions.DeadlockDetected("simulated store blip mid-replace")
        return await self._conn.executemany(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


@pg
@pytest.mark.asyncio
async def test_pgvector_replace_document_lands_only_the_new_chunks():
    schema = "repldoc_" + uuid.uuid4().hex[:8]
    p = await _provider(schema)
    try:
        store = p.get_vector_store()
        await store.create_collection("c", dimensions=4)
        for i in range(3):
            await store.put(_rec("d1", str(i)))
        # Shrink: the old higher-indexed chunks must be gone.
        await store.replace_document("c", "d1", [_rec("d1", "0", text="new")])
        out = await store.get("c", "d1")
        assert [r.chunk_id for r in out] == ["0"] and out[0].text == "new"
        # Grow: the stored set is overwritten wholesale.
        await store.replace_document("c", "d1", [_rec("d1", str(i)) for i in range(2)])
        assert [r.chunk_id for r in await store.get("c", "d1")] == ["0", "1"]
    finally:
        await p.aclose()
        await _drop_schema(schema)


@pg
@pytest.mark.asyncio
async def test_pgvector_a_failed_replace_leaves_the_old_chunks_searchable():
    schema = "repldoc_" + uuid.uuid4().hex[:8]
    p = await _provider(schema)
    real_pool = p.pool
    p._pool = _BlippingPool(real_pool)
    try:
        store = p.get_vector_store()
        await store.create_collection("c", dimensions=4)
        for i in range(3):
            await store.put(_rec("d1", str(i), text=f"old-{i}"))
        assert len(await store.get("c", "d1")) == 3
        with pytest.raises(PrimerError):
            await store.replace_document("c", "d1", [_rec("d1", "0", text="new")])
        after = await store.get("c", "d1")
        assert sorted(r.text for r in after) == ["old-0", "old-1", "old-2"]
    finally:
        await real_pool.close()
        await _drop_schema(schema)


@pg
@pytest.mark.asyncio
async def test_pgvector_a_rejected_replace_writes_nothing():
    schema = "repldoc_" + uuid.uuid4().hex[:8]
    p = await _provider(schema)
    try:
        store = p.get_vector_store()
        await store.create_collection("c", dimensions=4)
        for i in range(3):
            await store.put(_rec("d1", str(i)))
        with pytest.raises(BadRequestError):
            await store.replace_document("c", "d1", [_rec("d1", "0", n=2)])  # wrong width
        assert len(await store.get("c", "d1")) == 3  # the rejection happened before any write
    finally:
        await p.aclose()
        await _drop_schema(schema)


# ---------- lance: delete-then-merge (no cross-operation transaction) ----------

lancedb = pytest.importorskip("lancedb")  # type: ignore[attr-defined]


@pytest_asyncio.fixture
async def lance_store(tmp_path):
    p = LanceVectorStoreProvider(LanceConfig(path=tmp_path / "lance"))
    await p.initialize()
    try:
        yield p.get_vector_store()
    finally:
        await p.aclose()


def _lrec(document_id: str, chunk_id: str, *, text: str = "t") -> EmbeddingRecord:
    return _rec(document_id, chunk_id, n=3, text=text, collection_id="col-a")


@pytest.mark.asyncio
async def test_lance_replace_document_shrink_and_grow(lance_store):
    await lance_store.create_collection("col-a", dimensions=3)
    for i in range(3):
        await lance_store.put(_lrec("d1", str(i)))
    # Shrink: the old higher-indexed chunks must be gone.
    await lance_store.replace_document("col-a", "d1", [_lrec("d1", "0", text="only")])
    out = await lance_store.get("col-a", "d1")
    assert [r.chunk_id for r in out] == ["0"] and out[0].text == "only"
    # Grow: the stored set is overwritten wholesale.
    await lance_store.replace_document("col-a", "d1", [_lrec("d1", str(i)) for i in range(2)])
    assert [r.chunk_id for r in await lance_store.get("col-a", "d1")] == ["0", "1"]


@pytest.mark.asyncio
async def test_lance_replacing_with_nothing_removes_the_document(lance_store):
    await lance_store.create_collection("col-a", dimensions=3)
    for i in range(2):
        await lance_store.put(_lrec("d1", str(i)))
    await lance_store.replace_document("col-a", "d1", [])
    assert await lance_store.get("col-a", "d1") == []
