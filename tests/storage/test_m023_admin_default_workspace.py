"""Migration 23: the install default workspace becomes the first admin's default; golden characterization (PERF-T16)."""

from __future__ import annotations

from pathlib import Path

import pytest

from primer.bootstrap.prime import resolve_default_workspace_id
from primer.storage.migrations._rows import read_all
from primer.storage.migrations.m023_admin_default_workspace import M023AdminDefaultWorkspace, User, Workspace
from tests.storage._migration_golden import assert_golden, bulk, insert_raw


async def _old_apply(sp) -> None:
    """Migration 23 as it was before PERF-T16, frozen: read every user into a list, then decide."""
    users = sp.get_storage(User)
    rows = await read_all(users)
    admins = [u for u in rows if u.role == "admin" and not u.disabled and u.created_at is not None]
    install_id = await resolve_default_workspace_id(sp)
    if not admins or install_id is None:
        return
    first = min(admins, key=lambda u: u.created_at)
    workspace = await sp.get_storage(Workspace).get(install_id)
    if workspace is None or first.default_workspace_id is not None:
        return
    if any(u.default_workspace_id == install_id for u in rows):
        return
    owners = (workspace.access or {}).get("owners") or []
    if owners and first.id not in owners:
        return
    await users.update(first.model_copy(update={"default_workspace_id": install_id}))


def _seed(case: str):
    async def seed(sp) -> None:
        users = {
            "admin-new": {"role": "admin", "created_at": "2025-06-01T00:00:00Z", "username": "n"},
            "admin-off": {"role": "admin", "disabled": True, "created_at": "2024-01-01T00:00:00Z"},
            "admin-old": {"role": "admin", "created_at": "2025-01-01T00:00:00Z", "n": 1.5},
            "admin-undated": {"role": "admin"},
            # The first admin is the oldest even with a later id: the election must see every user, across pages.
            "zz-admin-oldest": {"role": "admin", "created_at": "2024-06-01T00:00:00Z"},
            **bulk("ub", 450, lambda i: {"role": "user", "created_at": "2025-02-01T00:00:00Z"}),
        }
        if case == "no-admin":
            users = {k: {**v, "role": "user"} for k, v in users.items()}
        if case == "admin-has-default":
            users["zz-admin-oldest"]["default_workspace_id"] = "elsewhere"
        if case == "taken":
            users["ub449"]["default_workspace_id"] = "ws-prime"
        await insert_raw(sp, User, users)
        owners = ["someone"] if case == "owners-exclude" else ["zz-admin-oldest"]
        await insert_raw(sp, Workspace, {"ws-prime": {"access": {"owners": owners}, "name": "prime"}, "ws-other": {}})
        if case != "no-install":
            await sp.set_default_workspace_id("ws-prime")
    return seed


@pytest.mark.parametrize(
    ("case", "changes"),
    [("writes", True), ("taken", False), ("admin-has-default", False), ("owners-exclude", False), ("no-admin", False), ("no-install", False)],
)
async def test_golden_rows_match_the_old_implementation_and_a_second_run_changes_nothing(tmp_path: Path, case: str, changes: bool) -> None:
    await assert_golden(tmp_path, _seed(case), (User, Workspace), _old_apply, M023AdminDefaultWorkspace().apply, expect_change=changes)
