"""W55f: a grep ``glob`` holding ``/`` matches the path relative to the search root; one without ``/`` the basename."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import primer.workspace  # noqa: F401  (ToolCallContext model_rebuild)
from primer.workspace.local.tools import Grep, GrepArgs
from primer.workspace.sandbox.fake import FakeSandbox
from primer.workspace.sandbox.tools import SandboxGrep

_CTX = SimpleNamespace(session=SimpleNamespace(was_read=lambda p: True, mark_read=lambda p: None))


def _tree(root: Path) -> None:
    (root / "src" / "deep").mkdir(parents=True)
    for rel in ("top.py", "src/a.py", "src/deep/b.py", "src/c.txt"):
        (root / rel).write_text("hit\n")


def _local(root: Path):
    return Grep(root)


def _sandbox(root: Path):
    return SandboxGrep(FakeSandbox(root), workspace_root="/workspace")


@pytest.mark.parametrize("make", [_local, _sandbox], ids=["local", "sandbox"])
@pytest.mark.parametrize(("glob", "path", "expected"), [
    ("**/*.py", ".", "src/a.py\nsrc/deep/b.py\ntop.py"),
    ("src/*.py", ".", "src/a.py\nsrc/deep/b.py"),
    ("src/**/*.py", ".", "src/a.py\nsrc/deep/b.py"),
    ("deep/*.py", "src", "src/deep/b.py"),
    ("*.txt", ".", "src/c.txt"),
])
async def test_glob_filter(tmp_path: Path, make, glob: str, path: str, expected: str) -> None:
    _tree(tmp_path)
    res = await make(tmp_path).execute(GrepArgs(pattern="hit", glob=glob, path=path), _CTX)
    assert "\n".join(sorted(res.output.split("\n"))) == expected
