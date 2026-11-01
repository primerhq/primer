"""W55g: an empty old_string is refused (it used to insert new_string between every character)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import primer.workspace  # noqa: F401  (ToolCallContext model_rebuild)
from primer.model.except_ import BadRequestError
from primer.workspace.local.tools import Edit, EditArgs
from primer.workspace.sandbox.fake import FakeSandbox
from primer.workspace.sandbox.tools import SandboxEdit

_CTX = SimpleNamespace(session=SimpleNamespace(was_read=lambda p: True, mark_read=lambda p: None))


@pytest.mark.parametrize("make", [
    lambda root: Edit(root),
    lambda root: SandboxEdit(FakeSandbox(root), workspace_root="/workspace"),
], ids=["local", "sandbox"])
@pytest.mark.parametrize("body", [b"abc\n", b""])
async def test_empty_old_string_is_refused(tmp_path: Path, make, body: bytes) -> None:
    (tmp_path / "f.txt").write_bytes(body)
    with pytest.raises(BadRequestError, match="old_string must not be empty"):
        await make(tmp_path).execute(EditArgs(path="f.txt", old_string="", new_string="X", replace_all=True), _CTX)
    assert (tmp_path / "f.txt").read_bytes() == body
