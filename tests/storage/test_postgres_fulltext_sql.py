"""Deferred X2.5 (unit): the Postgres full-text search ranks only a bounded
candidate set, not every matching row.

The match is index-backed (the GIN index on the expression
``to_tsvector('english', content)`` answers the inner select unchanged), and
the outer pass - the only place a body is re-parsed (ts_rank scoring) -
sees at most ``_FULLTEXT_CANDIDATE_CAP`` documents. The live behaviour (the
DDL untouched, the cap bounding a match beyond it, exact narrow ranking) is
pinned against a real Postgres in
tests/storage/test_postgres_fulltext_candidate_cap_live.py; this file pins
the query itself without a database.
"""

from __future__ import annotations

from primer.storage import postgres as pg

_CAP = pg._FULLTEXT_CANDIDATE_CAP  # noqa: SLF001 - the cap is the contract here


class _Conn:
    def __init__(self):
        self.fetches: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *args):
        self.fetches.append((sql, args))
        return []


async def test_the_search_query_ranks_only_a_bounded_candidate_set():
    conn = _Conn()
    store = pg.PostgresDocumentContentStore(pool=object(), schema="s")
    rows = await store.search_fulltext("kb", "refund policy", conn=conn)
    assert rows == []
    assert len(conn.fetches) == 1
    sql, args = conn.fetches[0]
    # The match is the index's own expression (so the GIN index answers it),
    # and the candidate select is capped at the module constant.
    assert "to_tsvector('english', content) @@ websearch_to_tsquery('english', $2)" in sql
    assert f"LIMIT {_CAP}" in sql
    # Exactly one unbounded-looking pass touches bodies: the rank (and the
    # excerpt headline) run over the capped candidates only - one inner
    # match select, one outer scoring select.
    assert sql.count("websearch_to_tsquery('english', $2)") == 3  # match, headline, rank
    assert "ts_rank(" in sql
    assert "ts_headline('english', d.content" in sql
    # The cap is a constant of the query, not a user parameter.
    assert args == ("kb", "refund policy", None, 20)
