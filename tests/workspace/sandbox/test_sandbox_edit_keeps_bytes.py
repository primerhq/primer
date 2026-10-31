"""W17: a sandbox edit changes only the replaced text; bytes that are not UTF-8 survive it."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import primer.workspace  # noqa: F401  (ToolCallContext model_rebuild)
from primer.workspace.local.tools import EditArgs
from primer.workspace.sandbox.fake import FakeSandbox
from primer.workspace.sandbox.tools import SandboxEdit

_CTX = SimpleNamespace(session=SimpleNamespace(was_read=lambda p: True, mark_read=lambda p: None))


async def test_latin1_bytes_survive_an_edit(tmp_path: Path) -> None:
    (tmp_path / "l1.txt").write_bytes("caf\xe9\nname=x\n".encode("latin-1"))
    tool = SandboxEdit(FakeSandbox(tmp_path), workspace_root="/workspace")
    res = await tool.execute(EditArgs(path="l1.txt", old_string="name=x", new_string="name=y"), _CTX)
    assert (tmp_path / "l1.txt").read_bytes() == "caf\xe9\nname=y\n".encode("latin-1")
    # The diff shows an undecodable byte as U+FFFD, as before, and is valid UTF-8.
    assert res.output == "--- l1.txt\n+++ l1.txt\n@@ -1,2 +1,2 @@\n caf�\n-name=x\n+name=y"
    res.output.encode("utf-8")
