"""Regression for the "Delete failed" 500 on the k8s/sandbox backend.

The runtime ``delete`` op removes a single file or an EMPTY directory only --
it is NOT recursive, so ``SandboxWorkspace.delete_file(recursive=True)`` used
to hand a non-empty directory straight to it and 500. ``_delete_tree`` now
empties the tree child-first so every ``delete`` lands on a leaf.

Uses a stub sandbox whose ``delete`` faithfully refuses a non-empty directory
(mirroring the real runtime); the ``FakeSandbox`` can't reproduce the bug
because its ``delete`` does ``shutil.rmtree``.
"""
from __future__ import annotations

import types

import pytest

from primer.model.except_ import BadRequestError
from primer.workspace.sandbox.workspace import SandboxWorkspace


class _NonRecursiveSandbox:
    """Deletes a file or an *empty* dir; raises on a non-empty dir."""

    def __init__(self) -> None:
        # absolute path -> "dir" | "file"
        self.tree: dict[str, str] = {
            "/w/d": "dir",
            "/w/d/sub": "dir",
            "/w/d/sub/f1.txt": "file",
            "/w/d/f2.txt": "file",
        }
        self.deleted: list[str] = []

    async def list_dir(self, path: str):
        prefix = path.rstrip("/") + "/"
        return [
            types.SimpleNamespace(path=p, kind=kind)
            for p, kind in self.tree.items()
            if p.startswith(prefix) and "/" not in p[len(prefix):]
        ]

    async def delete(self, path: str) -> None:
        kind = self.tree.get(path)
        assert kind is not None, f"delete of missing path {path!r}"
        if kind == "dir":
            prefix = path.rstrip("/") + "/"
            if any(p != path and p.startswith(prefix) for p in self.tree):
                raise OSError(f"directory not empty: {path}")
        del self.tree[path]
        self.deleted.append(path)


async def test_delete_tree_empties_children_before_each_dir() -> None:
    stub = _NonRecursiveSandbox()
    ws = SandboxWorkspace.__new__(SandboxWorkspace)
    ws._sandbox = stub  # type: ignore[attr-defined]

    await ws._delete_tree("/w/d")

    # Nothing left behind, and no "directory not empty" error was raised.
    assert stub.tree == {}
    # Depth-first: a directory is only deleted after its descendants.
    assert stub.deleted.index("/w/d/sub/f1.txt") < stub.deleted.index("/w/d/sub")
    assert stub.deleted.index("/w/d/sub") < stub.deleted.index("/w/d")
    assert stub.deleted[-1] == "/w/d"


async def test_write_state_file_rejects_root_escape() -> None:
    # The privileged .state write must still stay inside the workspace root
    # (_resolve_path rejects ``..``/absolute); only _refuse_reserved is skipped.
    ws = SandboxWorkspace.__new__(SandboxWorkspace)
    ws._workspace_root = "/workspace"  # type: ignore[attr-defined]
    with pytest.raises(BadRequestError):
        await ws.write_state_file("../evil.json", b"x")
