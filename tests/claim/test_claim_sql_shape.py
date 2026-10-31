"""The shape of the Postgres claim query (perf plan Task 3, finding H4). Pure SQL-text tests: they run without a database.

The old per-kind CTE filtered ``(claimed_by IS NULL OR expires_at < now())``. The OR can match rows the partial index on unclaimed leases does not
hold, so the planner could not use it. Each kind now selects two disjoint branches, unclaimed and expired, each ordered and limited and each locked in
its own FROM-subquery (``FOR UPDATE`` is not allowed over a UNION itself), and the outer query keeps the global order and limit.
"""
from __future__ import annotations

import re

import pytest

from primer.claim.adapters.harnesses import HarnessClaimAdapter
from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.sql import build_claim_query
from primer.int.claim import ClaimKind

LEASES = '"test"."leases"'


def _sql(*kinds: ClaimKind) -> str:
    adapters = {
        ClaimKind.HARNESS: HarnessClaimAdapter(harness_storage=None),
        ClaimKind.SESSION: SessionClaimAdapter(session_storage=None),
    }
    return build_claim_query({k: adapters[k] for k in kinds}, LEASES, schema="test")


def _cte(sql: str, name: str) -> str:
    """The body of one ``<name> AS (...)`` CTE (parentheses balanced)."""
    start = sql.index(f"{name} AS (") + len(f"{name} AS (")
    depth, i = 1, start
    while depth:
        depth += {"(": 1, ")": -1}.get(sql[i], 0)
        i += 1
    return sql[start : i - 1]


@pytest.mark.parametrize("kinds", [(ClaimKind.SESSION,), (ClaimKind.HARNESS, ClaimKind.SESSION)])
def test_each_kind_selects_an_unclaimed_branch_and_an_expired_branch(kinds):
    sql = _sql(*kinds)
    for kind in kinds:
        body = _cte(sql, f"{kind.value}_cand")
        assert body.count("UNION ALL") == 1, body
        assert "claimed_by IS NULL OR" not in body and "OR l.expires_at" not in body, "the OR that defeats the partial index is back"
        unclaimed, expired = body.split("UNION ALL")
        assert "l.claimed_by IS NULL" in unclaimed and "expires_at" not in unclaimed
        assert "l.claimed_by IS NOT NULL" in expired and "l.expires_at < now()" in expired
        for branch in (unclaimed, expired):
            assert "l.next_attempt_at <= now()" in branch
            assert re.search(r"ORDER BY l\.priority_score, l\.next_attempt_at\s+LIMIT \$1 FOR UPDATE OF l SKIP LOCKED", branch), branch


def test_each_branch_locks_inside_its_own_from_subquery_never_over_the_union():
    body = _cte(_sql(ClaimKind.SESSION), "session_cand")
    # FOR UPDATE directly over a UNION is a Postgres error; the lock sits in "FROM ( ... FOR UPDATE ... ) alias" under each branch.
    assert body.count("FOR UPDATE OF l SKIP LOCKED") == 2
    assert len(re.findall(r"FROM \(\s*SELECT l\.kind", body)) == 2
    tail = body.rsplit("SKIP LOCKED", 1)[1]
    assert "FOR UPDATE" not in tail and "UNION" not in tail  # nothing is locked at the set-operation level


def test_the_outer_query_still_orders_and_limits_across_kinds_and_binds_every_parameter():
    sql = _sql(ClaimKind.HARNESS, ClaimKind.SESSION)
    assert re.search(r"all_cand AS \(.*ORDER BY priority_score, next_attempt_at LIMIT \$1\)", sql, re.S)
    for n in (1, 2, 3):
        assert f"${n}" in sql


def test_the_claim_still_joins_the_entity_table_and_applies_the_adapters_eligibility_in_both_branches():
    body = _cte(_sql(ClaimKind.SESSION), "session_cand")
    assert body.count('JOIN "test"."sessions" e ON e.id = l.entity_id') == 2
    eligibility = SessionClaimAdapter(session_storage=None).eligibility_sql()
    assert body.count(f"({eligibility})") == 2
