"""W16 (named triggers): glob, grep and edit answer a tool error or skip, never raise a non-PrimerError.

A non-``PrimerError`` from a workspace tool fails the whole turn. The triggers named by the review: a glob or grep
hit through a symlink that leaves the root, ``glob("../*")``, a local edit of a non-UTF-8 file, and a sandbox grep
entry whose read fails (a symlinked directory the runtime lists as a file).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import primer.workspace  # noqa: F401  (ToolCallContext model_rebuild)
from primer.model.except_ import BadRequestError
from primer.workspace.local.tools import Edit, EditArgs, Glob, GlobArgs, Grep, GrepArgs
from primer.workspace.runtime.runtime_client import RuntimeError as RuntimeOpError
from primer.workspace.sandbox.fake import FakeSandbox
from primer.workspace.sandbox.tools import SandboxGrep

_CTX = SimpleNamespace(session=SimpleNamespace(was_read=lambda p: True, mark_read=lambda p: None))


async def test_glob_skips_a_symlink_leaving_the_root(tmp_path: Path) -> None:
    (tmp_path / "outside.txt").write_text("x")
    root = tmp_path / "ws"
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / "a.py").write_text("x")
    (root / ".venv" / "bin" / "python").symlink_to(tmp_path / "outside.txt")
    res = await Glob(root).execute(GlobArgs(pattern="**/*"), _CTX)
    assert set(res.output.split("\n")) == {".venv", ".venv/bin", "a.py"}


async def test_glob_dotdot_returns_nothing_outside(tmp_path: Path) -> None:
    (tmp_path / "secret-name.txt").write_text("x")
    root = tmp_path / "ws"
    root.mkdir()
    res = await Glob(root).execute(GlobArgs(pattern="../*.txt"), _CTX)
    assert res.output == ""


@pytest.mark.parametrize("pattern", ["/etc/*", "**foo"])
async def test_glob_invalid_pattern_is_a_bad_request(tmp_path: Path, pattern: str) -> None:
    with pytest.raises(BadRequestError, match="glob pattern"):
        await Glob(tmp_path).execute(GlobArgs(pattern=pattern), _CTX)


async def test_grep_skips_a_symlink_leaving_the_root(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text("TODO outside\n")
    root = tmp_path / "ws"
    root.mkdir()
    (root / "notes.md").symlink_to(tmp_path / "notes.md")
    (root / "inside.md").write_text("TODO inside\n")
    res = await Grep(root).execute(GrepArgs(pattern="TODO", output_mode="content"), _CTX)
    assert res.output == "inside.md:1:TODO inside"


async def test_local_edit_of_non_utf8_is_a_bad_request(tmp_path: Path) -> None:
    body = "caf\xe9 x\n".encode("latin-1")
    (tmp_path / "l1.txt").write_bytes(body)
    with pytest.raises(BadRequestError, match="not UTF-8"):
        await Edit(tmp_path).execute(EditArgs(path="l1.txt", old_string="x", new_string="y"), _CTX)
    assert (tmp_path / "l1.txt").read_bytes() == body


class _ListsALinkAsAFile(FakeSandbox):
    """The runtime lists a symlinked directory as a file; reading it answers EISDIR."""

    def __init__(self, root: Path, failing: dict[str, Exception]) -> None:
        super().__init__(root)
        self._failing = failing

    async def read_file(self, path: str) -> bytes:
        for suffix, exc in self._failing.items():
            if path.endswith(suffix):
                raise exc
        return await super().read_file(path)


async def test_sandbox_grep_skips_an_entry_whose_read_fails(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("TODO a\n")
    (tmp_path / "linkdir").write_text("TODO never read\n")
    (tmp_path / "gone").write_text("TODO never read\n")
    sb = _ListsALinkAsAFile(tmp_path, {
        "/linkdir": RuntimeOpError("EISDIR", "is a directory"),
        "/gone": FileNotFoundError("gone"),
    })
    res = await SandboxGrep(sb, workspace_root="/workspace").execute(GrepArgs(pattern="TODO"), _CTX)
    assert res.output == "a.txt"


async def test_sandbox_grep_still_raises_a_runtime_failure(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("TODO a\n")
    sb = _ListsALinkAsAFile(tmp_path, {"/a.txt": RuntimeOpError("ETIMEDOUT", "runtime timed out")})
    with pytest.raises(RuntimeOpError):
        await SandboxGrep(sb, workspace_root="/workspace").execute(GrepArgs(pattern="TODO"), _CTX)
