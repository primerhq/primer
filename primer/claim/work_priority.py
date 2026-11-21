"""Entity-owned priority snapshots, never a producer-supplied lease override."""

from primer.model.work_priority import validate_work_priority


def priority_sql(alias: str = "e") -> str:
    """Missing legacy field is zero; malformed JSON produces NULL and refuses arm.

    The bounded decimal spelling makes the cast safe even for hostile JSON.
    NULL must not be COALESCE'd by callers: it is not foreground permission.
    """
    value = f"{alias}.data->>'priority'"
    return (
        f"CASE WHEN NOT ({alias}.data ? 'priority') THEN 0 "
        f"WHEN jsonb_typeof({alias}.data->'priority') = 'number' "
        f"AND ({value}) ~ '^(0|[1-9][0-9]{{0,6}})$' THEN "
        f"CASE WHEN ({value})::int <= 1000000 THEN ({value})::int END END"
    )


async def read_priority(storage, entity_id: str) -> int:
    if storage is None:
        raise ValueError("work priority requires authoritative entity storage")
    entity = await storage.get(entity_id)
    if entity is None:
        raise ValueError("cannot arm work priority for a missing entity")
    return validate_work_priority(getattr(entity, "priority", 0))
