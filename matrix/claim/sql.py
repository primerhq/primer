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

from matrix.int.claim import ClaimAdapter


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
        Schema-qualified table reference, e.g. ``'"matrix"."leases"'``.
    schema:
        Optional schema name used to qualify entity tables in JOINs.
        When None, entity tables are referenced without schema prefix
        (works only when Postgres search_path includes the schema).
    """
    if not adapters:
        # No adapters → produce a valid but no-op UPDATE that always
        # returns zero rows. This keeps claim_due safe when called with
        # an empty adapter registry.
        return (
            f"WITH all_cand AS ("
            f"  SELECT kind, entity_id, priority_score, next_attempt_at "
            f"  FROM {leases_table} WHERE FALSE"
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
        ctes.append(
            f"{adapter.kind.value}_cand AS ("
            f"  SELECT l.kind, l.entity_id, l.priority_score, l.next_attempt_at"
            f"    FROM {leases_table} l"
            f"    JOIN {entity_ref} e ON e.id = l.entity_id"
            f"   WHERE l.kind = '{adapter.kind.value}'"
            f"     AND (l.claimed_by IS NULL OR l.expires_at < now())"
            f"     AND l.next_attempt_at <= now()"
            f"     AND ({adapter.eligibility_sql()})"
            f"   ORDER BY l.priority_score, l.next_attempt_at"
            f"   LIMIT $1 FOR UPDATE OF l SKIP LOCKED"
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
