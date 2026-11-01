"""Migration 13: stored ``config.hnsw_ef_search = 40`` becomes ``100`` on semantic-search providers; golden characterization (PERF-T16)."""

from __future__ import annotations

from pathlib import Path

from primer.model.storage import OffsetPage
from primer.storage.migrations.m013_hnsw_ef_search_default import M013HnswEfSearchDefault, SemanticSearchProvider
from tests.storage._migration_golden import assert_golden, bulk, insert_raw


async def _old_apply(sp) -> None:
    """Migration 13 as it was before PERF-T16, frozen: OFFSET pages, a field-scoped ``patch_if`` per row at 40."""
    providers = sp.get_storage(SemanticSearchProvider)
    offset = 0
    while True:
        page = await providers.list(OffsetPage(offset=offset, length=200, want_total=False))
        for row in page.items:
            if not isinstance(row.config, dict):
                continue
            ef = row.config.get("hnsw_ef_search")
            if isinstance(ef, bool) or ef != 40:
                continue
            await providers.patch_if(row.id, set_paths={("config", "hnsw_ef_search"): 100}, where={("config", "hnsw_ef_search"): [40]})
        if len(page.items) < 200:
            break
        offset += 200


async def _seed(sp) -> None:
    await insert_raw(sp, SemanticSearchProvider, {
        "p-40": {"provider": "pgvector", "config": {"hnsw_ef_search": 40, "password": "pw", "m": 16}},
        "p-40f": {"provider": "lance", "config": {"hnsw_ef_search": 40.0}},
        "p-true": {"config": {"hnsw_ef_search": True}},
        "p-100": {"config": {"hnsw_ef_search": 100}},
        "p-str": {"config": {"hnsw_ef_search": "40"}},
        "p-none": {"config": {"hnsw_ef_search": None}},
        "p-nokey": {"config": {"m": 16}},
        "p-null": {"config": None},
        "p-absent": {"provider": "lance"},
        **bulk("pb", 450, lambda i: {"provider": "pgvector", "config": {"hnsw_ef_search": 40 if i % 3 else 64}}),
    })


async def test_golden_rows_match_the_old_implementation_and_a_second_run_changes_nothing(tmp_path: Path) -> None:
    await assert_golden(tmp_path, _seed, (SemanticSearchProvider,), _old_apply, M013HnswEfSearchDefault().apply)
