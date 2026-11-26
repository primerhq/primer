"""The paged ``read`` breaks lines the way text mode does: ``\\n``, ``\\r\\n`` and a lone ``\\r`` -- NOT
``\\x0c``, ``\\x85`` or ``\\u2028`` (unlike ``str.splitlines()``). That is the paging contract both halves of the
read tool serve (C1 deferred, documented in X3.4): the local read iterates in text mode (universal newlines) and
the sandbox read settles line breaks from raw bytes, so a file whose lines carry form feeds, the C1 control or
a line separator keeps the same line numbers on both. Before the local read moved to text-mode splitting its
line numbers followed ``str.splitlines()`` and would have diverged from the sandbox path for such files.
"""

from __future__ import annotations

from pathlib import Path

from primer.workspace.local.tools.read import Read, ReadArgs
from primer.workspace.sandbox.fake import FakeSandbox
from primer.workspace.sandbox.tools.read import SandboxRead

from tests.workspace.sandbox.test_sandbox_1_8_tools import _ctx


async def test_form_feed_and_line_separators_do_not_break_a_line(tmp_path: Path) -> None:
    """``\\x0c``, ``\\x85`` and ``\\u2028`` stay inside the line: ONE numbered line on the local path and the
    sandbox path alike (``str.splitlines`` would count this file as four lines)."""
    body = "a\x0cb\x85c\u2028d\n"
    assert len(body.splitlines()) == 4  # the old local behaviour, kept for contrast
    (tmp_path / "f.txt").write_bytes(body.encode())
    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/f.txt", body.encode())

    local = await Read(tmp_path).execute(ReadArgs(path="f.txt"), _ctx())
    assert local.output == f"     1\u2192{body.rstrip(chr(10))}"

    sandbox = await SandboxRead(sb, workspace_root="/workspace").execute(
        ReadArgs(path="f.txt"), _ctx()
    )
    assert sandbox.output == local.output
    assert sandbox.truncated is False


async def test_crlf_and_lone_cr_break_lines_on_both_paths(tmp_path: Path) -> None:
    """The text-mode breaks themselves: CRLF and a lone CR are line breaks (a final CRLF ends the file, as in
    text mode), on both paths with the same numbers."""
    body = b"x\r\ny\rm\r\n"
    (tmp_path / "c.txt").write_bytes(body)
    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/c.txt", body)

    local = await Read(tmp_path).execute(ReadArgs(path="c.txt"), _ctx())
    assert local.output == "     1\u2192x\n     2\u2192y\n     3\u2192m"

    sandbox = await SandboxRead(sb, workspace_root="/workspace").execute(
        ReadArgs(path="c.txt"), _ctx()
    )
    assert sandbox.output == local.output
    assert sandbox.truncated is False
