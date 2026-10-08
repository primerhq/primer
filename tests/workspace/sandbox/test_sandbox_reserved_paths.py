"""Sandbox workspaces refuse the reserved trees however the path is spelled (FS-07).

``SandboxWorkspace`` used to compare the RAW path string against
``.state`` / ``.tmp``, so ``./.state/x``, ``x/../.state/x`` or
``/.state/x`` (which ``_resolve_path`` anchors at the workspace root) got
through to the sandbox. The check now runs on the normalised path, the
same way ``LocalWorkspace`` compares resolved paths.
"""
from __future__ import annotations

import types

import pytest

from primer.model.except_ import BadRequestError
from primer.workspace.sandbox.workspace import SandboxWorkspace


class _RecordingSandbox:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def stat(self, path: str):
        return None

    async def write_file(self, path: str, content: bytes) -> None:
        self.calls.append(("write", path))

    async def make_dir(self, path: str) -> None:
        self.calls.append(("mkdir", path))

    async def delete(self, path: str) -> None:
        self.calls.append(("delete", path))


def _ws() -> tuple[SandboxWorkspace, _RecordingSandbox]:
    ws = SandboxWorkspace.__new__(SandboxWorkspace)
    sandbox = _RecordingSandbox()
    ws._sandbox = sandbox  # type: ignore[attr-defined]
    ws._workspace_root = "/workspace"  # type: ignore[attr-defined]
    ws._template = types.SimpleNamespace(state_path=".state", tmp_path=".tmp")  # type: ignore[attr-defined]
    return ws, sandbox


_RESERVED_SPELLINGS = [
    ".state",
    ".state/sessions/s1/session.json",
    "./.state/sessions/s1/session.json",
    "x/../.state/sessions/s1/session.json",
    "/.state/mounts.json",
    ".state//mounts.json",
    "./.tmp/scratch",
    "a/b/../../.tmp",
    ".\\.state\\mounts.json",
]


@pytest.mark.parametrize("path", _RESERVED_SPELLINGS)
async def test_write_file_refuses_every_spelling_of_a_reserved_tree(path: str) -> None:
    ws, sandbox = _ws()
    with pytest.raises(BadRequestError, match="reserved tree"):
        await ws.write_file(path, b"x")
    assert sandbox.calls == []


@pytest.mark.parametrize("path", _RESERVED_SPELLINGS)
async def test_make_dir_refuses_every_spelling_of_a_reserved_tree(path: str) -> None:
    ws, sandbox = _ws()
    with pytest.raises(BadRequestError, match="reserved tree"):
        await ws.make_dir(path)
    assert sandbox.calls == []


@pytest.mark.parametrize("path", _RESERVED_SPELLINGS)
async def test_delete_file_refuses_every_spelling_of_a_reserved_tree(path: str) -> None:
    ws, sandbox = _ws()
    with pytest.raises(BadRequestError, match="reserved tree"):
        await ws.delete_file(path)
    assert sandbox.calls == []


@pytest.mark.parametrize("path", [".stateful/x.txt", "notes/.state", "x/.tmp/y", ".tmpfile"])
async def test_lookalike_names_outside_the_reserved_trees_are_written(path: str) -> None:
    ws, sandbox = _ws()
    await ws.write_file(path, b"x")
    assert sandbox.calls == [("write", f"/workspace/{path}")]


class _DirSandbox(_RecordingSandbox):
    """Every path is an (empty) directory, so a recursive delete would reach the sandbox."""

    async def stat(self, path: str):
        return types.SimpleNamespace(path=path, kind="dir")

    async def list_dir(self, path: str):
        return []


@pytest.mark.parametrize("path", [".", "/", "x/..", "./", "a/b/../.."])
async def test_delete_file_refuses_the_workspace_root(path: str) -> None:
    """A path that normalises to the root would take .state and .tmp with it (LocalWorkspace refuses it too)."""
    ws, _ = _ws()
    sandbox = _DirSandbox()
    ws._sandbox = sandbox  # type: ignore[attr-defined]
    with pytest.raises(BadRequestError, match="workspace root"):
        await ws.delete_file(path, recursive=True)
    assert sandbox.calls == []
