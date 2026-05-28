"""Tests for SandboxWorkspace against FakeSandbox."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from primer.model.workspace import (
    WorkspaceStatus,
    WorkspaceTemplate,
    ContainerTemplateConfig,
)
from primer.workspace.sandbox.fake import FakeSandbox
from primer.workspace.sandbox.workspace import SandboxWorkspace


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git CLI not available on PATH (SandboxStateRepo needs it)",
)


def _template() -> WorkspaceTemplate:
    return WorkspaceTemplate(
        id="t1", provider_id="c1", description="",
        backend=ContainerTemplateConfig(image="python:3.13"),
    )


@pytest.mark.asyncio
async def test_status_ready(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    ws = await SandboxWorkspace.materialise(
        workspace_id="ws-1", template=_template(),
        sandbox=sb, backend_kind="container",
    )
    s = await ws.status()
    assert isinstance(s, WorkspaceStatus)
    assert s.state == "ready"
    assert s.backend == "container"


@pytest.mark.asyncio
async def test_get_tools_returns_seven(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    ws = await SandboxWorkspace.materialise(
        workspace_id="ws-1", template=_template(),
        sandbox=sb, backend_kind="container",
    )
    tools = ws.get_tools()
    ids = sorted(t.id for t in tools)
    assert ids == ["edit", "exec", "glob", "grep", "ls", "read", "write"]


@pytest.mark.asyncio
async def test_read_write_file(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    ws = await SandboxWorkspace.materialise(
        workspace_id="ws-1", template=_template(),
        sandbox=sb, backend_kind="container",
    )
    await ws.write_file("hello.txt", b"world")
    assert await ws.read_file("hello.txt") == b"world"


@pytest.mark.asyncio
async def test_list_files_recursive(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    ws = await SandboxWorkspace.materialise(
        workspace_id="ws-1", template=_template(),
        sandbox=sb, backend_kind="container",
    )
    await ws.write_file("a.txt", b"1")
    await ws.write_file("dir/b.txt", b"2")
    entries = await ws.list_files(".", recursive=True)
    paths = sorted(e.path for e in entries)
    assert "a.txt" in paths
    assert any(p.endswith("b.txt") for p in paths)


@pytest.mark.asyncio
async def test_refuses_writes_under_state(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    ws = await SandboxWorkspace.materialise(
        workspace_id="ws-1", template=_template(),
        sandbox=sb, backend_kind="container",
    )
    from primer.model.except_ import BadRequestError
    with pytest.raises(BadRequestError):
        await ws.write_file(".state/sneaky", b"x")


@pytest.mark.asyncio
async def test_status_kubernetes_backend_label(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    ws = await SandboxWorkspace.materialise(
        workspace_id="ws-2", template=_template(),
        sandbox=sb, backend_kind="kubernetes",
    )
    s = await ws.status()
    assert s.backend == "kubernetes"


# ===========================================================================
# SandboxWorkspace.append_message_line
# ===========================================================================


@pytest.mark.asyncio
async def test_append_message_line_creates_file(tmp_path: Path) -> None:
    """First append creates messages.jsonl in the right sandbox path."""
    sb = FakeSandbox(root=tmp_path)
    ws = await SandboxWorkspace.materialise(
        workspace_id="ws-aml-1", template=_template(),
        sandbox=sb, backend_kind="container",
    )
    sid = "sess-aml-1"
    await ws.append_message_line(sid, b'{"seq":1,"kind":"done"}\n')

    # FakeSandbox maps /workspace/<...> to tmp_path/<...>
    # Path pattern: <workspace_root>/<state_path>/sessions/<sid>/messages.jsonl
    # With workspace_root=/workspace and state_path=.state:
    #   /workspace/.state/sessions/sess-aml-1/messages.jsonl
    #   => tmp_path/.state/sessions/sess-aml-1/messages.jsonl
    expected = tmp_path / ".state" / "sessions" / sid / "messages.jsonl"
    assert expected.exists()
    assert expected.read_bytes() == b'{"seq":1,"kind":"done"}\n'


@pytest.mark.asyncio
async def test_append_message_line_appends_sequentially(tmp_path: Path) -> None:
    """Multiple appends accumulate correctly."""
    sb = FakeSandbox(root=tmp_path)
    ws = await SandboxWorkspace.materialise(
        workspace_id="ws-aml-2", template=_template(),
        sandbox=sb, backend_kind="container",
    )
    sid = "sess-aml-2"
    line1 = b'{"seq":1,"kind":"user_input"}\n'
    line2 = b'{"seq":2,"kind":"done"}\n'

    await ws.append_message_line(sid, line1)
    await ws.append_message_line(sid, line2)

    path = tmp_path / ".state" / "sessions" / sid / "messages.jsonl"
    assert path.read_bytes() == line1 + line2


@pytest.mark.asyncio
async def test_append_message_line_adds_trailing_newline(tmp_path: Path) -> None:
    """Line without trailing newline gets one added."""
    sb = FakeSandbox(root=tmp_path)
    ws = await SandboxWorkspace.materialise(
        workspace_id="ws-aml-3", template=_template(),
        sandbox=sb, backend_kind="container",
    )
    sid = "sess-aml-3"
    await ws.append_message_line(sid, b'{"seq":1}')

    path = tmp_path / ".state" / "sessions" / sid / "messages.jsonl"
    assert path.read_bytes().endswith(b"\n")


@pytest.mark.asyncio
async def test_append_message_line_noop_for_empty(tmp_path: Path) -> None:
    """Appending empty bytes is a no-op."""
    sb = FakeSandbox(root=tmp_path)
    ws = await SandboxWorkspace.materialise(
        workspace_id="ws-aml-4", template=_template(),
        sandbox=sb, backend_kind="container",
    )
    sid = "sess-aml-4"
    await ws.append_message_line(sid, b"")

    path = tmp_path / ".state" / "sessions" / sid / "messages.jsonl"
    assert not path.exists()
