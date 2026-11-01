"""SQL builder for the Postgres claim-due query.

:func:`build_claim_query` composes one CTE per registered adapter
(each CTE filters the ``leases`` table + JOINs the entity table for
eligibility), unions them with ``UNION ALL``, then drives a single
``UPDATE … RETURNING`` to atomically claim all rows.

The returned SQL uses numbered bind parameters ($1–$3):
  $1  max_count  (integer)
  $2  worker_id  (text)
  $3  ttl_seconds (text, e.g. ``"60"``)
"""

from __future__ import annotations

from primer.int.claim import ClaimAdapter


def build_claim_query(
    adapters: dict,  # dict[ClaimKind, ClaimAdapter]
    leases_table: str,
    *,
    schema: str | None = None,
) -> str:
    """Compose CTEs + UNION ALL + final UPDATE returning claimed leases.

    Parameters
    ----------
    adapters:
        Mapping of ClaimKind → ClaimAdapter instances.
    leases_table:
        Schema-qualified table reference, e.g. ``'"primer"."leases"'``.
    schema:
        Optional schema name used to qualify entity tables in JOINs.
        When None, entity tables are referenced without schema prefix
        (works only when Postgres search_path includes the schema).
    """
    if not adapters:
        # No adapters → produce a valid but no-op UPDATE that always
        # returns zero rows. This keeps claim_due safe when called with
        # an empty adapter registry.
        #
        # It must still reference $1: claim_due always binds all three
        # parameters, and Postgres cannot infer a type for a bound
        # parameter that appears nowhere in the statement (asyncpg raises
        # IndeterminateDatatypeError). The LIMIT gives $1 its integer type
        # and costs nothing on a WHERE FALSE scan.
        return (
            f"WITH all_cand AS ("
            f"  SELECT kind, entity_id, priority_score, next_attempt_at "
            f"  FROM {leases_table} WHERE FALSE LIMIT $1"
            f") "
            f"UPDATE {leases_table} l "
            f"   SET claimed_by = $2, claimed_at = now(), "
            f"       last_heartbeat_at = now(), "
            f"       expires_at = now() + ($3 || ' seconds')::interval "
            f"  FROM all_cand a "
            f" WHERE l.kind = a.kind AND l.entity_id = a.entity_id "
            f"RETURNING l.kind, l.entity_id, l.claimed_at, l.expires_at, "
            f"          l.attempt_count, l.last_error"
        )

    def _entity_ref(entity_table: str) -> str:
        if schema:
            return f'"{schema}"."{entity_table}"'
        return entity_table

    ctes: list[str] = []
    for adapter in adapters.values():
        entity_ref = _entity_ref(adapter.entity_table)
        kind = adapter.kind.value

        def _branch(claim_state: str, alias: str, entity_ref: str = entity_ref, kind: str = kind, adapter: ClaimAdapter = adapter) -> str:
            # Each branch is locked in its own FROM-subquery: FOR UPDATE cannot be applied to a UNION. ``claim_state`` is one of the two disjoint
            # conditions below, each served by its own partial index (leases_unclaimed_by_kind / leases_expiry_by_kind).
            return (
                f"SELECT kind, entity_id, priority_score, next_attempt_at FROM ("
                f"  SELECT l.kind, l.entity_id, l.priority_score, l.next_attempt_at"
                f"    FROM {leases_table} l"
                f"    JOIN {entity_ref} e ON e.id = l.entity_id"
                f"   WHERE l.kind = '{kind}'"
                f"     AND {claim_state}"
                f"     AND l.next_attempt_at <= now()"
                f"     AND ({adapter.eligibility_sql()})"
                f"   ORDER BY l.priority_score, l.next_attempt_at"
                f"   LIMIT $1 FOR UPDATE OF l SKIP LOCKED"
                f") {alias}"
            )

        # Unclaimed OR expired, as two branches: the OR on one scan could not use the partial index on unclaimed leases. The branches are disjoint, and
        # the best $1 of their union are the best $1 of the old single scan, so the claim is the same set in the same order.
        ctes.append(
            f"{kind}_cand AS ("
            f"{_branch('l.claimed_by IS NULL', 'unclaimed')}"
            f" UNION ALL "
            f"{_branch('l.claimed_by IS NOT NULL AND l.expires_at < now()', 'expired')}"
            f")"
        )

    union_parts = " UNION ALL ".join(
        f"SELECT * FROM {a.kind.value}_cand" for a in adapters.values()
    )

    return (
        f"WITH {', '.join(ctes)}, "
        f"all_cand AS ({union_parts} ORDER BY priority_score, next_attempt_at LIMIT $1) "
        f"UPDATE {leases_table} l "
        f"   SET claimed_by = $2, claimed_at = now(), "
        f"       last_heartbeat_at = now(), "
        f"       expires_at = now() + ($3 || ' seconds')::interval "
        f"  FROM all_cand a "
        f" WHERE l.kind = a.kind AND l.entity_id = a.entity_id "
        f"RETURNING l.kind, l.entity_id, l.claimed_at, l.expires_at, "
        f"          l.attempt_count, l.last_error"
    )
