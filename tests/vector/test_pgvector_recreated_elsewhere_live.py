"""KN-18 on a live Postgres: two stores over one database, A drops and re-creates a collection at another width, B's next search works.

Gated on the single Postgres test gate (``tests/pg_gate.py``); ``tests/vector`` is a ``LANE_DIRS`` directory, so the CI Postgres lane runs it.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from urllib.parse import urlparse

import pytest
import pytest_asyncio

from primer.model.providers.vector import PgVectorConfig
from primer.model.vector import EmbeddingRecord
from primer.vector.pgvector import PgVectorStoreProvider
from tests.pg_gate import explicit_port, postgres_marks, require_postgres_url

_WHAT = "the live pgvector re-create test"
pytestmark = [*postgres_marks(_WHAT), pytest.mark.asyncio]
_BOUND_S = 60.0


@pytest_asyncio.fixture
async def providers() -> AsyncIterator[tuple[PgVectorStoreProvider, PgVectorStoreProvider]]:
    url = urlparse(require_postgres_url(_WHAT))
    schema = f"rec{uuid.uuid4().hex[:16]}"

    def make() -> PgVectorStoreProvider:
        return PgVectorStoreProvider(PgVectorConfig(
            hostname=url.hostname or "localhost", port=explicit_port(url), username=url.username or "primer",
            password=url.password or "",  # type: ignore[arg-type]
            database=(url.path or "/").lstrip("/") or "postgres", db_schema=schema,
        ))

    a, b = make(), make()
    async with asyncio.timeout(_BOUND_S):
        await a.initialize()
        await b.initialize()
    try:
        yield a, b
    finally:
        async with asyncio.timeout(_BOUND_S):
            async with a.pool.acquire() as conn:
                await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            await a.aclose()
            await b.aclose()


async def test_b_searches_after_a_recreates_the_collection_at_another_width(providers) -> None:
    a, b = (p.get_vector_store() for p in providers)
    async with asyncio.timeout(_BOUND_S):
        await a.create_collection("c", dimensions=3, distance="cosine")
        await a.put(EmbeddingRecord(collection_id="c", document_id="d", chunk_id="0", text="t", vector=[1.0, 0.0, 0.0]))
        assert [h.record.document_id for h in await b.search("c", [1.0, 0.0, 0.0], 1)] == ["d"]      # B caches width 3

        await a.drop_collection("c")
        await a.create_collection("c", dimensions=5, distance="cosine")
        await a.put(EmbeddingRecord(collection_id="c", document_id="e", chunk_id="0", text="t", vector=[1.0, 0.0, 0.0, 0.0, 0.0]))

        hits = await b.search("c", [1.0, 0.0, 0.0, 0.0, 0.0], 1)                                       # B's cache says 3: one re-resolve
        assert [h.record.document_id for h in hits] == ["e"]
