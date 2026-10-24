"""The loser of two creates of one explicit workspace id never deletes the winner's files (round 2 of #917, item 10).

``WorkspaceRegistry.create`` writes the row last and, when the row write fails, tears the live instance down so it is not orphaned. With an
explicit id the doors check that the id is free first, but two creates can both pass that check: both materialise, and the second row write
conflicts. A local backend ADOPTS an existing workspace directory (``mkdir(exist_ok=True)``: a directory left by an earlier install must not
crash the seed), so the loser's "live instance" is the winner's directory, and the rollback deleted the winner's files under its live row. A
conflict on an explicit id is now never rolled back: the instance is the winner's.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from primer.api.registries import WorkspaceRegistry
from primer.model.except_ import ConflictError
from primer.model.workspace import (
    LocalWorkspaceConfig,
    Workspace,
    WorkspaceProvider,
    WorkspaceProviderType,
    WorkspaceTemplate,
)
from primer.toolset.workspaces import build_workspaces_toolset
from tests.api.test_workspaces import _SP

WID = "shared"


@pytest.fixture
async def world(tmp_path: Path):
    sp = _SP()
    await sp.get_storage(WorkspaceProvider).create(
        WorkspaceProvider(id="local-1", provider=WorkspaceProviderType.LOCAL, config=LocalWorkspaceConfig(root_path=str(tmp_path)))
    )
    template = WorkspaceTemplate(id="tpl-1", description="t", provider_id="local-1")
    await sp.get_storage(WorkspaceTemplate).create(template)
    registry = WorkspaceRegistry(sp)  # the real local backend
    try:
        yield sp, registry, template, tmp_path
    finally:
        await registry.aclose()


async def _winner(registry, template, root: Path) -> Path:
    await registry.create(template=template, workspace_id=WID)
    work = root / WID / "work.txt"
    work.write_text("the winner's work")
    return work


@pytest.mark.asyncio
async def test_a_second_create_of_one_id_is_refused_before_it_touches_the_winner(world, monkeypatch) -> None:
    sp, registry, template, root = world
    work = await _winner(registry, template, root)
    backend = await registry.get_backend("local-1")
    built: list[str] = []
    real_create = backend.create

    async def _spy(*args, **kwargs):
        built.append(kwargs.get("workspace_id"))
        return await real_create(*args, **kwargs)

    monkeypatch.setattr(backend, "create", _spy)

    with pytest.raises(ConflictError, match="already exists"):
        await registry.create(template=template, workspace_id=WID)

    assert built == [], "the loser materialised over the winner's directory (its files and init commands ran again)"
    assert work.read_text() == "the winner's work"
    assert (await sp.get_storage(Workspace).get(WID)) is not None


@pytest.mark.asyncio
async def test_two_racing_creates_of_one_id_leave_one_workspace_with_its_files(world) -> None:
    """Both passed the doors' "id is free" check: in one process they run one at a time, and the second is refused."""
    sp, registry, template, root = world

    results = await asyncio.gather(
        registry.create(template=template, workspace_id=WID), registry.create(template=template, workspace_id=WID), return_exceptions=True,
    )

    assert sorted(type(r).__name__ for r in results) == ["ConflictError", "Workspace"], results
    assert (root / WID / ".state").is_dir(), "the loser tore the winner's directory down"
    assert (await sp.get_storage(Workspace).get(WID)) is not None
    assert registry._creating == {}  # noqa: SLF001


@pytest.mark.asyncio
async def test_a_row_conflict_from_another_process_does_not_roll_back_the_winner(world) -> None:
    """The winner's row is written by another process after this create's own check: the row write conflicts, and the instance (the
    winner's directory, which the local backend adopted) is left alone."""
    sp, registry, template, root = world
    work = await _winner(registry, template, root)
    store = sp.get_storage(Workspace)
    real_get = store.get

    async def _not_yet(id, *args, **kwargs):  # noqa: A002
        return None      # this process's check ran before the other process wrote the winner's row

    store.get = _not_yet
    try:
        with pytest.raises(ConflictError):
            await registry.create(template=template, workspace_id=WID)
    finally:
        store.get = real_get

    assert work.read_text() == "the winner's work", "the loser's rollback deleted the winner's directory"


@pytest.mark.asyncio
async def test_the_tool_answers_the_loser_with_a_conflict(world) -> None:
    sp, registry, template, root = world
    work = await _winner(registry, template, root)
    toolset = build_workspaces_toolset(storage_provider=sp, workspace_registry=registry)
    # The tool checks the id first; a create in another process that raced past that check meets the conflict at the row write.
    real_get = sp.get_storage(Workspace).get

    async def _not_yet(id, *args, **kwargs):  # noqa: A002
        return None

    sp.get_storage(Workspace).get = _not_yet
    try:
        result = await toolset.call(tool_name="create_workspace", arguments={"id": WID, "template_id": "tpl-1"})
    finally:
        sp.get_storage(Workspace).get = real_get

    assert result.is_error and json.loads(result.output)["type"] == "conflict", result.output
    assert work.read_text() == "the winner's work"


@pytest.mark.asyncio
async def test_a_failed_row_write_without_an_id_still_rolls_back(world) -> None:
    """With no id the platform picked a fresh one, so the instance is the create's own: a failed row write still tears it down."""
    sp, registry, template, root = world
    store = sp.get_storage(Workspace)

    async def _broken(row):
        raise ConflictError("the row could not be written")

    store.create = _broken
    with pytest.raises(ConflictError):
        await registry.create(template=template)

    assert [p for p in root.iterdir() if p.is_dir()] == [], "an orphaned workspace directory was left behind"
