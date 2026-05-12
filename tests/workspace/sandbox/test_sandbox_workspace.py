"""Tests for SandboxWorkspace against FakeSandbox."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from matrix.model.workspace import (
    WorkspaceStatus,
    WorkspaceTemplate,
    ContainerTemplateConfig,
)
from matrix.workspace.sandbox.fake import FakeSandbox
from matrix.workspace.sandbox.workspace import SandboxWorkspace


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
    from matrix.model.except_ import BadRequestError
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
