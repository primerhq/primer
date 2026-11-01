"""Migration 12: each workspace without a template snapshot gets a copy of its template's document; golden characterization (PERF-T16)."""

from __future__ import annotations

from pathlib import Path

from primer.model.common import dump_for_storage
from primer.model.storage import OffsetPage
from primer.storage import raw_generation
from primer.storage.migrations.m012_workspace_template_snapshots import M012WorkspaceTemplateSnapshots, Workspace, WorkspaceTemplate
from tests.storage._migration_golden import assert_golden, bulk, insert_raw


async def _old_apply(sp) -> None:
    """Migration 12 as it was before PERF-T16, frozen: OFFSET pages, ``patch_if`` per row that has no snapshot."""
    workspaces = sp.get_storage(Workspace)
    templates = sp.get_storage(WorkspaceTemplate)
    offset = 0
    while True:
        page = await workspaces.list(OffsetPage(offset=offset, length=200, want_total=False))
        for row in page.items:
            if row.template_snapshot is not None:
                continue
            template = await templates.get(row.template_id)
            if template is None:
                continue
            await workspaces.patch_if(
                row.id,
                {"template_snapshot": dump_for_storage(template)},
                where={"template_id": [raw_generation(row, "template_id")], "template_snapshot": [None]},
            )
        if len(page.items) < 200:
            break
        offset += 200


async def _seed(sp) -> None:
    await insert_raw(sp, WorkspaceTemplate, {
        "tpl-a": {"backend": {"kind": "container", "image": "x"}, "env": {"K": "secret"}, "n": 1.5},
        "tpl-b": {},
    })
    await insert_raw(sp, Workspace, {
        "w1": {"name": "one", "template_id": "tpl-a"},
        "w2": {"template_id": "tpl-a", "template_snapshot": None},
        "w3": {"template_id": "tpl-gone"},
        "w4": {"template_id": "tpl-b", "template_snapshot": {"old": True}},
        "w5": {"template_id": "tpl-b", "template_snapshot": 0},
        "w6": {"template_id": "tpl-b", "template_snapshot": ""},
        **bulk("wb", 450, lambda i: {"template_id": "tpl-a" if i % 2 else "tpl-b", "big": 12345678901234567890}),
    })


async def test_golden_rows_match_the_old_implementation_and_a_second_run_changes_nothing(tmp_path: Path) -> None:
    await assert_golden(tmp_path, _seed, (Workspace, WorkspaceTemplate), _old_apply, M012WorkspaceTemplateSnapshots().apply)
