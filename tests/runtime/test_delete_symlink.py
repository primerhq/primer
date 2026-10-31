"""The runtime's ``delete`` removes a symlink as a link, not its target (review of workspaces, W15).

``delete`` resolved the path and then removed the RESOLVED target: a link to a file unlinked that file, a link to an (empty) directory ``rmdir``'d it,
and the link was left dangling. Containment is still judged on where the entry lives; a link whose target is outside the workspace is removed as a
link, and the outside file is untouched. Regular files and empty directories behave as before.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

import primer_runtime.ops as ops
from primer_runtime.locks import WorkspaceLockTable


def _delete(path: str, root: Path) -> dict:
    return asyncio.run(ops.delete({"path": path}, str(root), WorkspaceLockTable()))


def test_a_symlink_to_a_file_is_unlinked_and_the_file_stays(tmp_path):
    (tmp_path / "real.txt").write_text("x")
    (tmp_path / "link.txt").symlink_to("real.txt")

    assert _delete("link.txt", tmp_path) == {"ok": True}

    assert not os.path.lexists(tmp_path / "link.txt") and (tmp_path / "real.txt").read_text() == "x"


def test_a_symlink_to_an_empty_directory_is_unlinked_and_the_directory_stays(tmp_path):
    (tmp_path / "emptydir").mkdir()
    (tmp_path / "current").symlink_to("emptydir")

    _delete("current", tmp_path)

    assert not os.path.lexists(tmp_path / "current") and (tmp_path / "emptydir").is_dir()


def test_a_dangling_symlink_can_be_deleted(tmp_path):
    (tmp_path / "gone").symlink_to("nowhere")

    _delete("gone", tmp_path)

    assert not os.path.lexists(tmp_path / "gone")


def test_a_symlink_to_a_file_outside_the_workspace_is_unlinked_as_a_link(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (root / "escape").symlink_to(outside)

    _delete("escape", root)

    assert not os.path.lexists(root / "escape") and outside.read_text() == "secret"


def test_a_path_through_a_symlinked_directory_that_leaves_the_workspace_is_still_refused(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f.txt").write_text("x")
    (root / "door").symlink_to(outside)

    with pytest.raises(ops.OpError):
        _delete("door/f.txt", root)
    assert (outside / "f.txt").exists()


def test_regular_files_and_empty_directories_still_delete_and_missing_still_raises(tmp_path):
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "d").mkdir()

    _delete("a.txt", tmp_path)
    _delete("d", tmp_path)
    assert not (tmp_path / "a.txt").exists() and not (tmp_path / "d").exists()
    with pytest.raises(ops.OpError):
        _delete("ghost.txt", tmp_path)


# ---- W55a: the runtime's atomic write gives a new file the umask mode too ---------------------------------------------------------------------


@pytest.mark.parametrize("umask, expected", [(0o022, 0o644), (0o077, 0o600)])
def test_a_new_file_written_by_the_runtime_gets_the_umask_mode(tmp_path, umask, expected):
    old = os.umask(umask)
    try:
        ops._atomic_write_bytes(tmp_path / "new.txt", b"x", None)
    finally:
        os.umask(old)

    assert (tmp_path / "new.txt").stat().st_mode & 0o777 == expected


def test_an_overwritten_file_and_an_explicit_mode_are_kept(tmp_path):
    target = tmp_path / "script.sh"
    target.write_bytes(b"old")
    target.chmod(0o755)

    ops._atomic_write_bytes(target, b"new", None)
    assert target.stat().st_mode & 0o777 == 0o755

    ops._atomic_write_bytes(tmp_path / "explicit.txt", b"x", 0o640)
    assert (tmp_path / "explicit.txt").stat().st_mode & 0o777 == 0o640
