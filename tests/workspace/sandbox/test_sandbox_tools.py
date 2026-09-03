"""Tests for the 7 sandbox-backed WorkspaceTool implementations."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from primer.model.except_ import BadRequestError, ConflictError, NotFoundError
from primer.workspace.sandbox.fake import FakeSandbox
from primer.workspace.sandbox.tools import (
    SandboxEdit,
    SandboxExec,
    SandboxGlob,
    SandboxGrep,
    SandboxLs,
    SandboxRead,
    SandboxWrite,
)
from primer.workspace.tool import ToolCallContext


class _StubSession:
    """Minimal stand-in for AgentSession in tool tests.

    Only carries the read-tracking surface that ``SandboxRead`` /
    ``SandboxWrite`` rely on.
    """

    def __init__(self) -> None:
        self._read: set[str] = set()

    def mark_read(self, path: str) -> None:
        self._read.add(path)

    def was_read(self, path: str) -> bool:
        return path in self._read


def _ctx() -> ToolCallContext:
    return ToolCallContext.model_construct(
        workspace_id="ws-1", session_id="sess-a", agent_id="agent-x",
        call_id="call-1", abort=asyncio.Event(),
        session=_StubSession(),  # type: ignore[arg-type]
    )


# ---- SandboxLs ------------------------------------------------------------


@pytest.mark.asyncio
async def test_ls_lists_workspace(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/a.txt", b"1")
    await sb.write_file("/workspace/b.txt", b"22")
    tool = SandboxLs(sb, workspace_root="/workspace")
    args = tool.parameters()(path=".")
    res = await tool.execute(args, _ctx())
    assert "a.txt" in res.output
    assert "b.txt" in res.output


@pytest.mark.asyncio
async def test_ls_not_found(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    tool = SandboxLs(sb, workspace_root="/workspace")
    args = tool.parameters()(path="missing")
    with pytest.raises(NotFoundError):
        await tool.execute(args, _ctx())


@pytest.mark.asyncio
async def test_ls_recursive_lists_subdirs(tmp_path: Path) -> None:
    """01a065f1: recursive=True used to be silently ignored (schema-
    visible, description-promised, never implemented) - a model
    requesting a recursive listing on a sandbox workspace silently got
    one level."""
    sb = FakeSandbox(root=tmp_path)
    await sb.make_dir("/workspace/src")
    await sb.write_file("/workspace/src/main.py", b"pass")
    tool = SandboxLs(sb, workspace_root="/workspace")
    args = tool.parameters()(path=".", recursive=True)
    res = await tool.execute(args, _ctx())
    assert "src/main.py" in res.output


@pytest.mark.asyncio
async def test_ls_non_recursive_does_not_descend(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    await sb.make_dir("/workspace/src")
    await sb.write_file("/workspace/src/main.py", b"pass")
    tool = SandboxLs(sb, workspace_root="/workspace")
    args = tool.parameters()(path=".")
    res = await tool.execute(args, _ctx())
    assert "src" in res.output
    assert "main.py" not in res.output


@pytest.mark.asyncio
async def test_ls_skips_dotfiles_by_default(tmp_path: Path) -> None:
    """01a065f1: show_hidden was ALSO silently ignored (dotfiles always
    shown) alongside recursive - fixed as part of implementing the
    shared LsArgs schema properly, not left half-done."""
    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/.hidden", b"x")
    await sb.write_file("/workspace/visible", b"x")
    tool = SandboxLs(sb, workspace_root="/workspace")
    args = tool.parameters()(path=".")
    res = await tool.execute(args, _ctx())
    assert ".hidden" not in res.output
    assert "visible" in res.output


@pytest.mark.asyncio
async def test_ls_show_hidden_includes_dotfiles(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/.env", b"x")
    tool = SandboxLs(sb, workspace_root="/workspace")
    args = tool.parameters()(path=".", show_hidden=True)
    res = await tool.execute(args, _ctx())
    assert ".env" in res.output


@pytest.mark.asyncio
async def test_ls_max_depth_caps_recursion(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    await sb.make_dir("/workspace/a/b/c")
    await sb.write_file("/workspace/a/b/c/leaf.txt", b"x")
    tool = SandboxLs(sb, workspace_root="/workspace")
    args = tool.parameters()(path=".", recursive=True, max_depth=1)
    res = await tool.execute(args, _ctx())
    # depth=1 includes 'a' and stops; 'b' and 'leaf.txt' should NOT appear.
    assert "a/b" not in res.output
    assert "leaf.txt" not in res.output


@pytest.mark.asyncio
async def test_ls_walk_reports_truncation_when_capped(
    tmp_path: Path, monkeypatch,
) -> None:
    import primer.workspace.sandbox.tools.ls as sandbox_ls_module

    monkeypatch.setattr(sandbox_ls_module, "_MAX_ENTRIES", 3)
    sb = FakeSandbox(root=tmp_path)
    for i in range(5):
        await sb.write_file(f"/workspace/f{i}.txt", str(i).encode())
    tool = SandboxLs(sb, workspace_root="/workspace")
    args = tool.parameters()(path=".")
    res = await tool.execute(args, _ctx())
    lines = res.output.splitlines()
    assert len(lines) == 4  # 3 entries + the trailing truncation line
    assert lines[-1] == "... truncated at 3 entries (of unknown more)"


@pytest.mark.asyncio
async def test_ls_walk_omits_truncation_line_under_the_cap(
    tmp_path: Path,
) -> None:
    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/a.txt", b"x")
    tool = SandboxLs(sb, workspace_root="/workspace")
    args = tool.parameters()(path=".")
    res = await tool.execute(args, _ctx())
    assert "truncated" not in res.output


@pytest.mark.asyncio
async def test_ls_cap_stops_descending_not_just_appending(
    tmp_path: Path, monkeypatch,
) -> None:
    import primer.workspace.sandbox.tools.ls as sandbox_ls_module

    monkeypatch.setattr(sandbox_ls_module, "_MAX_ENTRIES", 2)
    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/a.txt", b"x")
    await sb.write_file("/workspace/b.txt", b"x")
    await sb.make_dir("/workspace/sub/nested")
    await sb.write_file("/workspace/sub/nested/leaf.txt", b"x")
    tool = SandboxLs(sb, workspace_root="/workspace")
    args = tool.parameters()(path=".", recursive=True)
    res = await tool.execute(args, _ctx())
    assert "leaf.txt" not in res.output
    assert "nested" not in res.output
    assert "truncated at 2 entries" in res.output


# ---- SandboxRead ----------------------------------------------------------


@pytest.mark.asyncio
async def test_read_returns_numbered_lines(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/x.txt", b"alpha\nbeta\n")
    tool = SandboxRead(sb, workspace_root="/workspace")
    args = tool.parameters()(path="x.txt")
    res = await tool.execute(args, _ctx())
    assert "alpha" in res.output
    assert "beta" in res.output


@pytest.mark.asyncio
async def test_read_binary_returns_summary(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/x.bin", b"\x00\x01\x02hello")
    tool = SandboxRead(sb, workspace_root="/workspace")
    args = tool.parameters()(path="x.bin")
    res = await tool.execute(args, _ctx())
    assert "binary file" in res.output
    assert res.truncated is True


# ---- SandboxWrite ---------------------------------------------------------


@pytest.mark.asyncio
async def test_write_creates_file(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    tool = SandboxWrite(sb, workspace_root="/workspace")
    args = tool.parameters()(path="y.txt", content="hello")
    res = await tool.execute(args, _ctx())
    assert "wrote 5 bytes" in res.output
    assert await sb.read_file("/workspace/y.txt") == b"hello"


@pytest.mark.asyncio
async def test_write_refuses_unread_overwrite(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/y.txt", b"old")
    tool = SandboxWrite(sb, workspace_root="/workspace")
    args = tool.parameters()(path="y.txt", content="new")
    with pytest.raises(ConflictError):
        await tool.execute(args, _ctx())


@pytest.mark.asyncio
async def test_write_with_force_overwrites(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/y.txt", b"old")
    tool = SandboxWrite(sb, workspace_root="/workspace")
    args = tool.parameters()(path="y.txt", content="new", force=True)
    await tool.execute(args, _ctx())
    assert await sb.read_file("/workspace/y.txt") == b"new"


@pytest.mark.asyncio
async def test_write_new_file_metadata_is_all_additions(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    tool = SandboxWrite(sb, workspace_root="/workspace")
    args = tool.parameters()(path="y.txt", content="a\nb\nc")
    res = await tool.execute(args, _ctx())
    assert res.metadata == {"additions": 3, "deletions": 0}


@pytest.mark.asyncio
async def test_write_metadata_matches_the_local_backend_shape(
    tmp_path: Path,
) -> None:
    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/y.txt", b"a\nb\nc\n")
    tool = SandboxWrite(sb, workspace_root="/workspace")
    args = tool.parameters()(path="y.txt", content="a\nx\nc\nd\n", force=True)
    res = await tool.execute(args, _ctx())
    assert res.metadata == {"additions": 2, "deletions": 1}


# ---- SandboxEdit ----------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_replaces_text(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/x.txt", b"hello world\n")
    tool = SandboxEdit(sb, workspace_root="/workspace")
    args = tool.parameters()(
        path="x.txt", old_string="world", new_string="there",
    )
    await tool.execute(args, _ctx())
    body = await sb.read_file("/workspace/x.txt")
    assert body == b"hello there\n"


@pytest.mark.asyncio
async def test_edit_nonunique_rejected(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/x.txt", b"foo foo")
    tool = SandboxEdit(sb, workspace_root="/workspace")
    args = tool.parameters()(
        path="x.txt", old_string="foo", new_string="bar",
    )
    with pytest.raises(BadRequestError):
        await tool.execute(args, _ctx())


# ---- SandboxGlob ----------------------------------------------------------


@pytest.mark.asyncio
async def test_glob_finds_files(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/a.py", b"")
    await sb.write_file("/workspace/b.py", b"")
    await sb.write_file("/workspace/c.txt", b"")
    tool = SandboxGlob(sb, workspace_root="/workspace")
    args = tool.parameters()(pattern="*.py", path=".")
    res = await tool.execute(args, _ctx())
    assert "a.py" in res.output
    assert "b.py" in res.output
    assert "c.txt" not in res.output


@pytest.mark.asyncio
async def test_glob_recursive(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/src/main.py", b"")
    await sb.write_file("/workspace/src/util/helper.py", b"")
    tool = SandboxGlob(sb, workspace_root="/workspace")
    args = tool.parameters()(pattern="**/*.py", path=".")
    res = await tool.execute(args, _ctx())
    assert "main.py" in res.output
    assert "helper.py" in res.output


# ---- SandboxGrep ----------------------------------------------------------


@pytest.mark.asyncio
async def test_grep_finds_pattern(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/x.txt", b"needle here\nother line\n")
    tool = SandboxGrep(sb, workspace_root="/workspace")
    args = tool.parameters()(pattern="needle", path=".")
    res = await tool.execute(args, _ctx())
    assert "x.txt" in res.output


@pytest.mark.asyncio
async def test_grep_content_mode(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/x.txt", b"alpha\nbeta needle\ngamma\n")
    tool = SandboxGrep(sb, workspace_root="/workspace")
    args = tool.parameters()(
        pattern="needle", path=".", output_mode="content",
    )
    res = await tool.execute(args, _ctx())
    assert "x.txt:2:beta needle" in res.output


@pytest.mark.asyncio
async def test_grep_metadata_matches_the_local_backend_shape(tmp_path: Path) -> None:
    """UX reconcile wave 5: same match_count/file_count/truncated metadata
    as the local backend (primer/workspace/local/tools/grep.py) - a chip
    must read identically regardless of which backend ran the call."""
    sb = FakeSandbox(root=tmp_path)
    await sb.write_file("/workspace/a.txt", b"needle\nneedle\n")
    await sb.write_file("/workspace/b.txt", b"needle\n")
    await sb.write_file("/workspace/c.txt", b"nothing here\n")
    tool = SandboxGrep(sb, workspace_root="/workspace")
    args = tool.parameters()(pattern="needle", path=".")
    res = await tool.execute(args, _ctx())
    assert res.metadata == {
        "match_count": 3, "file_count": 2, "truncated": False,
    }


@pytest.mark.asyncio
async def test_grep_metadata_truncated_flag_survives_head_limit(
    tmp_path: Path,
) -> None:
    sb = FakeSandbox(root=tmp_path)
    for i in range(5):
        await sb.write_file(f"/workspace/f{i}.txt", b"needle\n")
    tool = SandboxGrep(sb, workspace_root="/workspace")
    args = tool.parameters()(pattern="needle", path=".", head_limit=2)
    res = await tool.execute(args, _ctx())
    assert res.truncated is True
    assert res.metadata == {
        "match_count": 5, "file_count": 5, "truncated": True,
    }


# ---- SandboxExec ----------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_runs_command(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    tool = SandboxExec(sb, workspace_root="/workspace")
    args = tool.parameters()(
        command="echo hi", workdir=".",
        timeout_ms=5000, description="say hi",
    )
    res = await tool.execute(args, _ctx())
    # First line is exit code, then stdout, then stderr.
    assert res.output.startswith("0\n")
    assert "hi" in res.output


@pytest.mark.asyncio
async def test_exec_background_rejected(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    tool = SandboxExec(sb, workspace_root="/workspace")
    args = tool.parameters()(
        command="echo hi", workdir=".",
        timeout_ms=1000, description="bg", background=True,
    )
    with pytest.raises(BadRequestError):
        await tool.execute(args, _ctx())
