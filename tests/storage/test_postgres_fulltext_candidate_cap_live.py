"""Deferred X2.5 against a real Postgres: the full-text search matches
through the existing GIN expression index (the DDL is untouched - no stored
column, no index change) and ranks only a bounded candidate set, so a body
is re-parsed only to score it, for at most the first
``_FULLTEXT_CANDIDATE_CAP`` index matches. A match beyond the cap still
yields a bounded, best-first page; a narrow query ranks exactly as before
the cap (all of its matches are candidates), through the index.

Gated on the single Postgres test gate (tests/pg_gate.py); the file sits
under tests/storage, which the CI Postgres lane runs as one process, so no
LANE_FILES entry is needed.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from primer.storage.postgres import PostgresStorageProvider, _FULLTEXT_CANDIDATE_CAP
from tests.pg_gate import postgres_marks, require_postgres_url
from tests.storage.test_system_state import _parse_postgres_url

pytestmark = postgres_marks("the candidate-cap full-text tests")

WHY = "the candidate-cap full-text tests"


def _provider(schema: str) -> PostgresStorageProvider:
    cfg = _parse_postgres_url(require_postgres_url(WHY)).model_copy(update={"db_schema": schema})
    return PostgresStorageProvider(cfg)


@pytest_asyncio.fixture
async def schema() -> AsyncIterator[str]:
    name = f"t{uuid.uuid4().hex[:16]}"
    yield name
    sp = _provider("public")
    await sp.initialize()
    try:
        async with sp.pool.acquire() as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
    finally:
        await sp.aclose()


async def _open(schema: str) -> PostgresStorageProvider:
    sp = _provider(schema)
    await sp.initialize()
    return sp


@pytest.mark.asyncio
async def test_the_ddl_is_unchanged(schema: str) -> None:
    """ensure_schema creates exactly the pre-change objects: the same five
    columns (no stored tsvector column) and the same three indexes (the FTS
    one is still the GIN on the expression, under its old name)."""
    sp = await _open(schema)
    try:
        await sp.get_content_store().ensure_schema()
        async with sp.pool.acquire() as conn:
            columns = {
                r["name"]
                for r in await conn.fetch(
                    "SELECT column_name AS name FROM information_schema.columns "
                    "WHERE table_schema = $1 AND table_name = 'document_content'",
                    sp.schema,
                )
            }
            indexes = {
                r["indexname"]: r["indexdef"]
                for r in await conn.fetch(
                    "SELECT indexname, indexdef FROM pg_indexes "
                    "WHERE schemaname = $1 AND tablename = 'document_content'",
                    sp.schema,
                )
            }
        assert columns == {
            "document_id", "collection_id", "path", "content", "updated_at",
        }, columns
        assert set(indexes) == {
            "document_content_pkey", "document_content_coll_path",
            "document_content_coll", "document_content_fts",
        }, set(indexes)
        fts = indexes["document_content_fts"]
        assert "using gin" in fts.lower()
        # The GIN is on the EXPRESSION (the planner's exact match against the
        # inner select), not on a column: a stored-column rewrite of the DDL
        # would show ``content_tsv`` here instead.
        assert "to_tsvector('english'::regconfig, content)" in fts
        assert "content_tsv" not in fts
    finally:
        await sp.aclose()


@pytest.mark.asyncio
async def test_a_match_beyond_the_candidate_cap_is_bounded(schema: str) -> None:
    """Every row matches (more than the candidate cap): the page is still
    bounded by ``limit``, every hit is a real match, and the ordering is
    best-first within the ranked candidates."""
    sp = await _open(schema)
    store = sp.get_content_store()
    n_matches = _FULLTEXT_CANDIDATE_CAP + 500
    try:
        await store.ensure_schema()
        async with sp.pool.acquire() as conn:
            # (n_matches is a test constant: inlined, like the cap in the
            # production query - a bind parameter here breaks generate_series
            # overload resolution.)
            await conn.execute(
                f"INSERT INTO \"{schema}\".document_content "
                "(document_id, collection_id, path, content) "
                f"SELECT 'd' || g, 'kb', 'docs/' || g, "
                f"repeat('zebra filler pad ', 30) FROM generate_series(1, {n_matches}) g"
            )
        hits = await store.search_fulltext("kb", "zebra", limit=50)
        assert len(hits) == 50
        assert all(h.path.startswith("docs/") for h in hits)
        assert all(h.score > 0 for h in hits)
        scores = [h.score for h in hits]
        assert scores == sorted(scores, reverse=True)
        assert len({h.path for h in hits}) == 50
    finally:
        await sp.aclose()


@pytest.mark.asyncio
async def test_narrow_queries_rank_exactly_as_before_through_the_index(schema: str) -> None:
    """A query whose matches are within the cap ranks exactly as the
    pre-cap query did (every match is a candidate), the match runs through
    the GIN expression index, and the excerpt still comes from the body."""
    sp = await _open(schema)
    store = sp.get_content_store()
    try:
        await store.ensure_schema()
        async with sp.pool.acquire() as conn:
            # 2000 filler documents; 3 of them carry the rare term, with
            # distinct occurrence counts (the ranking signal).
            await conn.execute(
                f"INSERT INTO \"{schema}\".document_content "
                "(document_id, collection_id, path, content) "
                "SELECT 'd' || g, 'kb', 'docs/' || g, "
                "repeat('filler pad word ', 30) FROM generate_series(1, 2000) g"
            )
            for i, n in enumerate((1, 5, 20)):
                await conn.execute(
                    f"UPDATE \"{schema}\".document_content SET content = content || "
                    f"repeat('zebra ', $1) WHERE document_id = 'd' || $2",
                    n, str(10 + i),
                )
        hits = await store.search_fulltext("kb", "zebra", limit=10)
        assert [h.path for h in hits] == ["docs/12", "docs/11", "docs/10"]
        assert all(h.score > 0 for h in hits)
        assert "zebra" in hits[0].excerpt.lower()
        # The match predicate resolves through the GIN expression index.
        sql = (
            "EXPLAIN (FORMAT JSON) SELECT path FROM \"" + schema + "\".document_content "
            "WHERE collection_id = 'kb' "
            "AND to_tsvector('english', content) @@ websearch_to_tsquery('english', 'zebra')"
        )
        async with sp.pool.acquire() as conn:
            plan = await conn.fetch(sql)
        text = str(plan)
        assert "Bitmap Index Scan" in text and "document_content_fts" in text, text
    finally:
        await sp.aclose()
