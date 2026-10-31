"""Local file operations on symlinks and new-file modes (review of workspaces, W15 and W55a).

* W15: deleting or moving a symlink acted on its TARGET (``delete_file('current', recursive=True)`` removed ``src`` and left a dangling link; a move
  moved the target). The containment check stays on the resolved path; the operation is done on the link itself.
* W55a: ``mkstemp`` made every new file 0600; a new file now gets ``0o666 & ~umask`` as a plain ``open`` would, and an overwritten file keeps its mode.
"""

from __future__ import annotations

import os
import stat

import pytest

from primer.model.except_ import BadRequestError, ConflictError, NotFoundError
from primer.model.workspace import ResourceLimits, WorkspaceTemplate
from primer.workspace import LocalWorkspace
from primer.workspace.local.workspace import _atomic_write_bytes


def _tpl() -> WorkspaceTemplate:
    return WorkspaceTemplate(id="dev", description="d", provider_id="local-1", resources=ResourceLimits())


@pytest.fixture
async def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return await LocalWorkspace.materialise(workspace_id="ws-1", root=root, template=_tpl(), env={})


# ---- W15: a symlink is deleted or moved as a link ------------------------------------------------------------------------------------------


async def test_deleting_a_symlink_to_a_directory_removes_the_link_and_keeps_the_target(ws):
    (ws.root / "src").mkdir()
    (ws.root / "src" / "keep.py").write_text("important")
    (ws.root / "current").symlink_to("src")

    await ws.delete_file("current", recursive=True)

    assert not os.path.lexists(ws.root / "current")
    assert (ws.root / "src" / "keep.py").read_text() == "important"


async def test_deleting_a_symlink_without_recursive_removes_just_the_link(ws):
    (ws.root / "src").mkdir()
    (ws.root / "src" / "keep.py").write_text("important")
    (ws.root / "current").symlink_to("src")

    await ws.delete_file("current")

    assert not os.path.lexists(ws.root / "current") and (ws.root / "src" / "keep.py").exists()


async def test_deleting_a_symlink_to_a_file_keeps_the_target(ws):
    (ws.root / "real.txt").write_text("x")
    (ws.root / "link.txt").symlink_to("real.txt")

    await ws.delete_file("link.txt")

    assert not os.path.lexists(ws.root / "link.txt") and (ws.root / "real.txt").read_text() == "x"


async def test_a_dangling_symlink_can_be_deleted(ws):
    (ws.root / "gone").symlink_to("nowhere")

    await ws.delete_file("gone")

    assert not os.path.lexists(ws.root / "gone")


async def test_a_symlink_pointing_outside_the_workspace_is_deleted_as_a_link_and_the_outside_file_survives(ws, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (ws.root / "escape").symlink_to(outside)

    await ws.delete_file("escape")

    assert not os.path.lexists(ws.root / "escape") and outside.read_text() == "secret"


async def test_moving_a_symlink_moves_the_link_and_keeps_the_target_in_place(ws):
    (ws.root / "real.txt").write_text("x")
    (ws.root / "link.txt").symlink_to("real.txt")

    await ws.move_file("link.txt", "renamed.txt")

    assert not os.path.lexists(ws.root / "link.txt")
    assert (ws.root / "renamed.txt").is_symlink() and os.readlink(ws.root / "renamed.txt") == "real.txt"
    assert (ws.root / "real.txt").read_text() == "x"


async def test_moving_a_symlink_to_a_directory_moves_the_link_only(ws):
    (ws.root / "src").mkdir()
    (ws.root / "src" / "a.txt").write_text("a")
    (ws.root / "current").symlink_to("src")

    await ws.move_file("current", "latest")

    assert (ws.root / "latest").is_symlink() and not os.path.lexists(ws.root / "current")
    assert (ws.root / "src" / "a.txt").read_text() == "a"


async def test_moving_onto_an_existing_symlink_still_conflicts(ws):
    (ws.root / "a.txt").write_text("a")
    (ws.root / "dangling").symlink_to("nowhere")

    with pytest.raises(ConflictError):
        await ws.move_file("a.txt", "dangling")


async def test_a_symlink_into_the_reserved_state_tree_is_deleted_as_a_link_not_refused_and_the_state_tree_survives(ws):
    (ws.root / "state-link").symlink_to(".state")

    await ws.delete_file("state-link")

    assert not os.path.lexists(ws.root / "state-link") and (ws.root / ".state").is_dir()


async def test_a_path_through_a_symlinked_directory_that_leaves_the_workspace_is_still_refused(ws, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f.txt").write_text("x")
    (ws.root / "door").symlink_to(outside)

    with pytest.raises(BadRequestError):
        await ws.delete_file("door/f.txt")
    assert (outside / "f.txt").exists()


# ---- characterization: regular files and directories behave as before ------------------------------------------------------------------------


async def test_deleting_a_regular_file_and_a_directory_still_works(ws):
    (ws.root / "a.txt").write_text("a")
    (ws.root / "d").mkdir()
    (ws.root / "d" / "x.txt").write_text("x")

    await ws.delete_file("a.txt")
    with pytest.raises(BadRequestError):
        await ws.delete_file("d")  # not empty
    await ws.delete_file("d", recursive=True)

    assert not (ws.root / "a.txt").exists() and not (ws.root / "d").exists()


async def test_delete_and_move_keep_their_refusals(ws):
    with pytest.raises(NotFoundError):
        await ws.delete_file("missing.txt")
    with pytest.raises(BadRequestError):
        await ws.delete_file(".state/anything")
    with pytest.raises(BadRequestError):
        await ws.delete_file("../escape")
    (ws.root / "a.txt").write_text("a")
    (ws.root / "b.txt").write_text("b")
    with pytest.raises(ConflictError):
        await ws.move_file("a.txt", "b.txt")
    with pytest.raises(BadRequestError):
        await ws.move_file("a.txt", ".state/a.txt")
    (ws.root / "dir").mkdir()
    with pytest.raises(BadRequestError):
        await ws.move_file("dir", "dir/inner")


async def test_moving_a_regular_file_and_directory_still_works(ws):
    (ws.root / "a.txt").write_text("a")
    (ws.root / "d").mkdir()
    (ws.root / "d" / "x.txt").write_text("x")

    await ws.move_file("a.txt", "sub/b.txt")
    await ws.move_file("d", "e")

    assert (ws.root / "sub" / "b.txt").read_text() == "a" and (ws.root / "e" / "x.txt").read_text() == "x"


# ---- W55a: new files get the umask mode, overwritten files keep theirs ------------------------------------------------------------------------


@pytest.mark.parametrize("umask, expected", [(0o022, 0o644), (0o077, 0o600), (0o002, 0o664)])
def test_a_new_file_gets_the_mode_a_plain_open_would(tmp_path, umask, expected):
    old = os.umask(umask)
    try:
        _atomic_write_bytes(tmp_path / "new.txt", b"x")
        (tmp_path / "plain.txt").write_bytes(b"x")
    finally:
        os.umask(old)

    assert stat.S_IMODE((tmp_path / "new.txt").stat().st_mode) == stat.S_IMODE((tmp_path / "plain.txt").stat().st_mode) == expected


def test_an_overwritten_file_keeps_its_mode(tmp_path):
    target = tmp_path / "script.sh"
    target.write_bytes(b"old")
    target.chmod(0o755)

    _atomic_write_bytes(target, b"new")

    assert stat.S_IMODE(target.stat().st_mode) == 0o755 and target.read_bytes() == b"new"
