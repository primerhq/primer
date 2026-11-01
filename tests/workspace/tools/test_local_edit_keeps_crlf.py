"""W18: a local edit keeps a CRLF file CRLF; old_string still matches on ``\\n``."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import primer.workspace  # noqa: F401  (ToolCallContext model_rebuild)
from primer.workspace.local.tools import Edit, EditArgs

_CTX = SimpleNamespace(session=SimpleNamespace(was_read=lambda p: True, mark_read=lambda p: None))


async def test_crlf_file_stays_crlf(tmp_path: Path) -> None:
    (tmp_path / "a.bat").write_bytes(b"@echo off\r\nset A=1\r\nset B=2\r\n")
    res = await Edit(tmp_path).execute(EditArgs(path="a.bat", old_string="A=1", new_string="A=9"), _CTX)
    assert (tmp_path / "a.bat").read_bytes() == b"@echo off\r\nset A=9\r\nset B=2\r\n"
    assert res.output == "--- a.bat\n+++ a.bat\n@@ -1,3 +1,3 @@\n @echo off\n-set A=1\n+set A=9\n set B=2"


async def test_multiline_old_string_matches_a_crlf_file(tmp_path: Path) -> None:
    (tmp_path / "a.bat").write_bytes(b"one\r\ntwo\r\n")
    await Edit(tmp_path).execute(EditArgs(path="a.bat", old_string="one\ntwo", new_string="one\n2\nthree"), _CTX)
    assert (tmp_path / "a.bat").read_bytes() == b"one\r\n2\r\nthree\r\n"


async def test_a_crlf_new_string_is_not_doubled(tmp_path: Path) -> None:
    (tmp_path / "a.bat").write_bytes(b"one\r\ntwo\r\n")
    await Edit(tmp_path).execute(EditArgs(path="a.bat", old_string="two", new_string="2\r\nthree"), _CTX)
    assert (tmp_path / "a.bat").read_bytes() == b"one\r\n2\r\nthree\r\n"
