"""Tests for the 7 sandbox-backed WorkspaceTool implementations."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from matrix.model.except_ import BadRequestError, ConflictError, NotFoundError
from matrix.workspace.sandbox.fake import FakeSandbox
from matrix.workspace.sandbox.tools import (
    SandboxEdit,
    SandboxExec,
    SandboxGlob,
    SandboxGrep,
    SandboxLs,
    SandboxRead,
    SandboxWrite,
)
from matrix.workspace.tool import ToolCallContext


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
