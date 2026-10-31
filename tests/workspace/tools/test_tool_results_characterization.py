"""Characterization: glob, grep and edit results for paths inside the root.

These pin today's observable output (text, order, metadata, bytes written)
for both backends, so the fixes for W16, W17, W18, W55e, W55f and W55g can
be shown not to move any result a well-formed call already gets.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import primer.workspace  # noqa: F401  (ToolCallContext model_rebuild)
from primer.model.except_ import BadRequestError
from primer.workspace.local.tools import Edit, EditArgs, Glob, GlobArgs, Grep, GrepArgs
from primer.workspace.sandbox.fake import FakeSandbox
from primer.workspace.sandbox.tools import SandboxEdit, SandboxGlob, SandboxGrep

_CTX = SimpleNamespace(session=SimpleNamespace(was_read=lambda p: True, mark_read=lambda p: None))


def _tree(root: Path) -> None:
    """a.py, notes.md, bin.dat (NUL), src/b.py, src/deep/d.py with fixed mtimes."""
    (root / "src" / "deep").mkdir(parents=True)
    files = {
        "a.py": (b"x = 1\nTODO one\n", 100),
        "notes.md": (b"TODO md\n", 200),
        "bin.dat": (b"\x00TODO\n", 250),
        "src/b.py": (b"TODO two\nTODO three\n", 300),
        "src/deep/d.py": (b"nothing\n", 400),
    }
    for rel, (body, mtime) in files.items():
        (root / rel).write_bytes(body)
        os.utime(root / rel, (mtime, mtime))


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "ws"
    r.mkdir()
    _tree(r)
    return r


# ---- local glob -------------------------------------------------------------


async def test_local_glob_recursive_newest_first(root: Path) -> None:
    res = await Glob(root).execute(GlobArgs(pattern="**/*.py"), _CTX)
    assert res.output == "src/deep/d.py\nsrc/b.py\na.py"
    assert res.truncated is False


async def test_local_glob_under_a_path_and_paged(root: Path) -> None:
    res = await Glob(root).execute(GlobArgs(pattern="**/*.py", path="src", limit=1, offset=1), _CTX)
    assert res.output == "src/b.py"
    assert res.truncated is True


async def test_local_glob_inside_symlink_reports_its_target(root: Path) -> None:
    (root / "link.py").symlink_to("src/b.py")
    res = await Glob(root).execute(GlobArgs(pattern="*.py"), _CTX)
    # A link resolving inside the root is listed by its resolved path.
    assert res.output == "src/b.py\na.py"


# ---- local grep -------------------------------------------------------------


async def test_local_grep_content_order_and_metadata(root: Path) -> None:
    res = await Grep(root).execute(GrepArgs(pattern="TODO", output_mode="content"), _CTX)
    assert res.output == "a.py:2:TODO one\nnotes.md:1:TODO md\nsrc/b.py:1:TODO two\nsrc/b.py:2:TODO three"
    assert res.metadata == {"match_count": 4, "file_count": 3, "truncated": False}


async def test_local_grep_glob_without_slash_matches_basename_at_any_depth(root: Path) -> None:
    res = await Grep(root).execute(GrepArgs(pattern="TODO|nothing", glob="*.py"), _CTX)
    assert res.output == "a.py\nsrc/b.py\nsrc/deep/d.py"


async def test_local_grep_glob_under_a_path(root: Path) -> None:
    res = await Grep(root).execute(GrepArgs(pattern="TODO", path="src", glob="b.*", output_mode="count"), _CTX)
    assert res.output == "src/b.py:2"


async def test_local_grep_inside_symlink_reports_its_target(root: Path) -> None:
    (root / "link.md").symlink_to("notes.md")
    res = await Grep(root).execute(GrepArgs(pattern="TODO md"), _CTX)
    assert res.output == "notes.md\nnotes.md"


# ---- local edit -------------------------------------------------------------

_DIFF = "--- f.py\n+++ f.py\n@@ -1,2 +1,2 @@\n def f():\n-    return 1\n+    return 2"


async def test_local_edit_utf8_lf_bytes_and_diff(tmp_path: Path) -> None:
    (tmp_path / "f.py").write_bytes(b"def f():\n    return 1\n")
    res = await Edit(tmp_path).execute(EditArgs(path="f.py", old_string="return 1", new_string="return 2"), _CTX)
    assert (tmp_path / "f.py").read_bytes() == b"def f():\n    return 2\n"
    assert res.output == _DIFF


async def test_local_edit_replace_all_non_ascii(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_bytes("café x\ncafé x\n".encode())
    await Edit(tmp_path).execute(EditArgs(path="f.txt", old_string="x", new_string="y", replace_all=True), _CTX)
    assert (tmp_path / "f.txt").read_bytes() == "café y\ncafé y\n".encode()


async def test_local_edit_errors(tmp_path: Path) -> None:
    (tmp_path / "f.py").write_bytes(b"x\nx\n")
    tool = Edit(tmp_path)
    with pytest.raises(BadRequestError, match="identical"):
        await tool.execute(EditArgs(path="f.py", old_string="x", new_string="x"), _CTX)
    with pytest.raises(BadRequestError, match=r"non-unique in 'f.py' \(2 occurrences\)"):
        await tool.execute(EditArgs(path="f.py", old_string="x", new_string="y"), _CTX)
    with pytest.raises(BadRequestError, match="old_string not found in 'f.py'"):
        await tool.execute(EditArgs(path="f.py", old_string="z", new_string="y"), _CTX)


# ---- sandbox ----------------------------------------------------------------


async def test_sandbox_edit_utf8_bytes_and_diff(tmp_path: Path) -> None:
    (tmp_path / "f.py").write_bytes(b"def f():\n    return 1\n")
    tool = SandboxEdit(FakeSandbox(tmp_path), workspace_root="/workspace")
    res = await tool.execute(EditArgs(path="f.py", old_string="return 1", new_string="return 2"), _CTX)
    assert (tmp_path / "f.py").read_bytes() == b"def f():\n    return 2\n"
    assert res.output == _DIFF


async def test_sandbox_edit_keeps_crlf_bytes(tmp_path: Path) -> None:
    (tmp_path / "a.bat").write_bytes(b"one\r\nA=1\r\n")
    tool = SandboxEdit(FakeSandbox(tmp_path), workspace_root="/workspace")
    await tool.execute(EditArgs(path="a.bat", old_string="A=1", new_string="A=9"), _CTX)
    assert (tmp_path / "a.bat").read_bytes() == b"one\r\nA=9\r\n"


async def test_sandbox_grep_content_order_and_metadata(root: Path) -> None:
    tool = SandboxGrep(FakeSandbox(root), workspace_root="/workspace")
    res = await tool.execute(GrepArgs(pattern="TODO", output_mode="content"), _CTX)
    assert res.output == "a.py:2:TODO one\nnotes.md:1:TODO md\nsrc/b.py:1:TODO two\nsrc/b.py:2:TODO three"
    assert res.metadata == {"match_count": 4, "file_count": 3, "truncated": False}


async def test_sandbox_grep_glob_without_slash_matches_basename(root: Path) -> None:
    tool = SandboxGrep(FakeSandbox(root), workspace_root="/workspace")
    res = await tool.execute(GrepArgs(pattern="TODO|nothing", glob="*.py"), _CTX)
    assert res.output == "a.py\nsrc/b.py\nsrc/deep/d.py"


async def test_sandbox_grep_single_file_target(root: Path) -> None:
    tool = SandboxGrep(FakeSandbox(root), workspace_root="/workspace")
    res = await tool.execute(GrepArgs(pattern="TODO", path="src/b.py", output_mode="count"), _CTX)
    assert res.output == "src/b.py:2"


async def test_sandbox_glob_unchanged(root: Path) -> None:
    tool = SandboxGlob(FakeSandbox(root), workspace_root="/workspace")
    res = await tool.execute(GlobArgs(pattern="**/*.py"), _CTX)
    assert res.output == "src/deep/d.py\nsrc/b.py\na.py"
