"""Golden characterization helpers for the data migrations (PERF-T16).

A golden test seeds the same documents into two SQLite databases, runs a frozen copy of a migration's old ``apply`` on one and the live
``apply`` on the other, and compares every row. Documents are written raw, so a key the seed leaves out is absent (a model dump would
write it as ``null``), and are compared in canonical form (sorted keys): two rows are equal iff they hold the same values of the same JSON
types. Key ORDER is not compared: a whole-row ``update`` rewrites the document in the view's field order while ``patch_if`` edits it in
place, and Postgres ``jsonb`` keeps no key order at all.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider


async def insert_raw(sp: SqliteStorageProvider, view: type, rows: dict[str, dict[str, Any]]) -> None:
    """Store each ``{id: document}`` exactly as given, bypassing the model dump."""
    store = sp.get_storage(view)
    await store.get("")  # creates the table
    await sp.connection.executemany(
        f'INSERT INTO "{store._table}" (id, data) VALUES (?, ?)',  # noqa: SLF001
        [(row_id, json.dumps(doc)) for row_id, doc in rows.items()],
    )
    await sp.connection.commit()


async def snapshot(sp: SqliteStorageProvider, *views: type) -> dict[str, list[tuple[str, str]]]:
    """Every row of each view's table, by id, as ``(id, canonical JSON)``."""
    out: dict[str, list[tuple[str, str]]] = {}
    for view in views:
        table = sp.get_storage(view)._table  # noqa: SLF001
        cur = await sp.connection.execute(f'SELECT id, data FROM "{table}" ORDER BY id')
        out[table] = [(row_id, json.dumps(json.loads(data), sort_keys=True)) for row_id, data in await cur.fetchall()]
    return out


Step = Callable[[SqliteStorageProvider], Awaitable[None]]


async def run_on_seed(path: Path, seed: Step, views: tuple[type, ...], *steps: Step) -> dict[str, list[tuple[str, str]]]:
    """Seed a fresh database at ``path``, run ``steps`` in order, and return the snapshot of ``views``."""
    sp = SqliteStorageProvider(SqliteConfig(path=path))
    await sp.initialize()
    try:
        await seed(sp)
        for step in steps:
            await step(sp)
        return await snapshot(sp, *views)
    finally:
        await sp.aclose()


async def assert_golden(
    tmp_path: Path, seed: Step, views: tuple[type, ...], old: Step, new: Step, *, expect_change: bool = True,
) -> None:
    """The new step leaves every row as the old one did on the same seed, changes something (unless the seed pins a no-op), and a second
    run changes nothing."""
    before = await run_on_seed(tmp_path / "before.sqlite", seed, views)
    by_old = await run_on_seed(tmp_path / "old.sqlite", seed, views, old)
    by_new = await run_on_seed(tmp_path / "new.sqlite", seed, views, new)
    twice = await run_on_seed(tmp_path / "twice.sqlite", seed, views, new, new)
    assert (by_old != before) is expect_change, "the seed does not exercise what it says"
    assert by_new == by_old
    assert twice == by_new


def bulk(prefix: str, n: int, doc: Callable[[int], dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """``n`` documents ``{prefix}{i:03d}``: enough rows to cross the 200-row page boundary while the step writes or deletes the rows it pages."""
    return {f"{prefix}{i:03d}": doc(i) for i in range(n)}


__all__ = ["assert_golden", "bulk", "insert_raw", "run_on_seed", "snapshot"]
