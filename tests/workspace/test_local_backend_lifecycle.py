"""Local workspace lifecycle: destroy without a cached handle, the create rollback, and re-attach (review W2, W53, W54).

* W2: ``destroy`` answered ``NotFoundError`` on a cache miss (right after a boot, or a delete run in a worker process) and the
  registry then deleted the row, so the directory and its ``.state`` history stayed; a later create of the same id (ids are
  client-chosen) adopted them. A cache miss now removes the directory, never following a symlink or an escaping id.
* W53: create adopts an existing directory, and its failure rollback deleted it, files and all. It now removes only a
  directory it made itself.
* W54: re-attach rebuilt the env from the template alone, so a workspace's own env overrides were gone after a restart and
  in every worker process.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from primer.api.registries.workspace_registry import WorkspaceRegistry
from primer.model.access import Access
from primer.model.except_ import BadRequestError, NotFoundError, ValidationError
from primer.model.workspace import (
    LocalTemplateConfig,
    LocalWorkspaceConfig,
    WorkspaceProvider,
    WorkspaceProviderType,
    WorkspaceTemplate,
    WorkspaceTemplateOverrides,
)
from primer.workspace.local.backend import LocalWorkspaceBackend

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git CLI not available on PATH (the state repo needs it)")


def _template(**kwargs) -> WorkspaceTemplate:
    return WorkspaceTemplate(id="tpl", provider_id="local-1", description="", backend=LocalTemplateConfig(), **kwargs)


# ---- characterization ------------------------------------------------------------------------------------------------------------


async def test_destroying_a_cached_workspace_removes_its_directory(tmp_path) -> None:
    backend = LocalWorkspaceBackend(tmp_path)
    ws = await backend.create(_template(), workspace_id="proj")
    (ws.root / "notes.txt").write_text("x")
    await backend.destroy("proj")
    assert not (tmp_path / "proj").exists()
    with pytest.raises(NotFoundError):
        await backend.destroy("proj")


async def test_destroying_what_is_not_on_disk_is_not_found(tmp_path) -> None:
    backend = LocalWorkspaceBackend(tmp_path)
    await backend.initialize()
    with pytest.raises(NotFoundError):
        await backend.destroy("never-made")


async def test_a_traversal_id_is_still_refused_by_create(tmp_path) -> None:
    backend = LocalWorkspaceBackend(tmp_path / "root")
    with pytest.raises(ValidationError):
        await backend.create(_template(), workspace_id="../outside")


async def test_a_failed_create_removes_the_directory_it_made(tmp_path) -> None:
    backend = LocalWorkspaceBackend(tmp_path)
    with pytest.raises(BadRequestError):
        await backend.create(_template(init_commands=["exit 3"]), workspace_id="proj")
    assert not (tmp_path / "proj").exists()


async def test_a_cache_miss_destroy_never_follows_a_symlinked_workspace(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep.txt").write_text("not ours to delete")
    os.symlink(elsewhere, root / "proj")
    backend = LocalWorkspaceBackend(root)
    await backend.initialize()
    with pytest.raises(NotFoundError):
        await backend.destroy("proj")
    assert (elsewhere / "keep.txt").read_text() == "not ours to delete"


async def test_a_cache_miss_destroy_never_follows_an_escaping_id(tmp_path) -> None:
    root = tmp_path / "root"
    (tmp_path / "victim").mkdir()
    (tmp_path / "victim" / "keep.txt").write_text("x")
    backend = LocalWorkspaceBackend(root)
    await backend.initialize()
    with pytest.raises((NotFoundError, ValidationError)):
        await backend.destroy("../victim")
    assert (tmp_path / "victim" / "keep.txt").exists()


# ---- W2: a destroy without a cached handle removes the directory -----------------------------------------------------------------


async def test_a_destroy_in_a_fresh_backend_removes_the_directory_and_a_same_id_create_starts_empty(tmp_path) -> None:
    a = LocalWorkspaceBackend(tmp_path)
    ws = await a.create(_template(), workspace_id="proj")
    (ws.root / "secret.txt").write_text("alice's data")
    b = LocalWorkspaceBackend(tmp_path)            # another process, or after a restart: an empty cache
    await b.destroy("proj")
    assert not (tmp_path / "proj").exists()

    ws2 = await b.create(_template(), workspace_id="proj")      # bob creates "proj"
    assert not (ws2.root / "secret.txt").exists()


# ---- W53: a failed create keeps a directory it adopted ---------------------------------------------------------------------------


async def test_a_failed_create_over_an_existing_directory_leaves_its_files(tmp_path) -> None:
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / "precious.txt").write_text("from an earlier install")
    backend = LocalWorkspaceBackend(tmp_path)
    with pytest.raises(BadRequestError):
        await backend.create(_template(init_commands=["exit 3"]), workspace_id="proj")
    assert (tmp_path / "proj" / "precious.txt").read_text() == "from an earlier install"
