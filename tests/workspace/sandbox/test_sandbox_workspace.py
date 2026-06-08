"""Tests for SandboxWorkspace against FakeSandbox."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pydantic import SecretStr

from primer.model.workspace import (
    WorkspaceRuntimeMeta,
    WorkspaceStatus,
    WorkspaceTemplate,
    ContainerTemplateConfig,
)
from primer.workspace.sandbox.fake import FakeSandbox
from primer.workspace.sandbox.workspace import SandboxWorkspace


def _runtime_meta(workspace_id: str = "ws-test") -> WorkspaceRuntimeMeta:
    return WorkspaceRuntimeMeta(
        url=f"ws://fake/{workspace_id}",
        token=SecretStr("fake-token"),
    )


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
        runtime_meta=_runtime_meta(),
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
        runtime_meta=_runtime_meta(),
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
        runtime_meta=_runtime_meta(),
    )
    await ws.write_file("hello.txt", b"world")
    assert await ws.read_file("hello.txt") == b"world"


@pytest.mark.asyncio
async def test_list_files_recursive(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    ws = await SandboxWorkspace.materialise(
        workspace_id="ws-1", template=_template(),
        sandbox=sb, backend_kind="container",
        runtime_meta=_runtime_meta(),
    )
    await ws.write_file("a.txt", b"1")
    await ws.write_file("dir/b.txt", b"2")
    entries = await ws.list_files(".", recursive=True)
    paths = sorted(e.path for e in entries)
    assert "a.txt" in paths
    assert any(p.endswith("b.txt") for p in paths)


@pytest.mark.asyncio
async def test_make_dir_then_listed(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    ws = await SandboxWorkspace.materialise(
        workspace_id="ws-1", template=_template(),
        sandbox=sb, backend_kind="container",
        runtime_meta=_runtime_meta(),
    )
    await ws.make_dir("src")
    info = await ws.file_info("src")
    assert info.kind == "dir"


@pytest.mark.asyncio
async def test_make_dir_conflict(tmp_path: Path) -> None:
    from primer.model.except_ import BadRequestError

    sb = FakeSandbox(root=tmp_path)
    ws = await SandboxWorkspace.materialise(
        workspace_id="ws-1", template=_template(),
        sandbox=sb, backend_kind="container",
        runtime_meta=_runtime_meta(),
    )
    await ws.make_dir("src")
    with pytest.raises(BadRequestError):
        await ws.make_dir("src")


@pytest.mark.asyncio
async def test_delete_dir_recursive_vs_refused(tmp_path: Path) -> None:
    from primer.model.except_ import BadRequestError

    sb = FakeSandbox(root=tmp_path)
    ws = await SandboxWorkspace.materialise(
        workspace_id="ws-1", template=_template(),
        sandbox=sb, backend_kind="container",
        runtime_meta=_runtime_meta(),
    )
    await ws.make_dir("src")
    await ws.write_file("src/a.txt", b"1")
    with pytest.raises(BadRequestError):
        await ws.delete_file("src")  # non-empty, no recursive
    await ws.delete_file("src", recursive=True)
    entries = await ws.list_files(".", recursive=True)
    assert not any(e.path.startswith("src") for e in entries)


@pytest.mark.asyncio
async def test_refuses_writes_under_state(tmp_path: Path) -> None:
    sb = FakeSandbox(root=tmp_path)
    ws = await SandboxWorkspace.materialise(
        workspace_id="ws-1", template=_template(),
        sandbox=sb, backend_kind="container",
        runtime_meta=_runtime_meta(),
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
        runtime_meta=_runtime_meta(),
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
        runtime_meta=_runtime_meta(),
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
        runtime_meta=_runtime_meta(),
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
        runtime_meta=_runtime_meta(),
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
        runtime_meta=_runtime_meta(),
    )
    sid = "sess-aml-4"
    await ws.append_message_line(sid, b"")

    path = tmp_path / ".state" / "sessions" / sid / "messages.jsonl"
    assert not path.exists()


@pytest.mark.asyncio
async def test_diagnostic_exec_delegates_to_sandbox(tmp_path: Path) -> None:
    """SandboxWorkspace.diagnostic_exec forwards to Sandbox.exec and
    re-wraps the ExecResult into a WorkspaceDiagnosticResult."""
    from primer.int.sandbox import ExecResult

    sb = FakeSandbox(root=tmp_path)
    ws = await SandboxWorkspace.materialise(
        workspace_id="ws-diag", template=_template(),
        sandbox=sb, backend_kind="container",
        runtime_meta=_runtime_meta(),
    )

    # Swap the sandbox's exec out AFTER materialise (which skips git init
    # because FakeSandbox satisfies _StateCapableSandbox). The diagnostic
    # call must then hit our recording stub.
    captured: dict = {}

    async def _recording_exec(
        command, *, workdir="/workspace", env=None,
        timeout_seconds=None, stdin=None, abort=None,
    ):
        captured["command"] = command
        captured["workdir"] = workdir
        captured["timeout_seconds"] = timeout_seconds
        return ExecResult(
            exit_code=0,
            stdout="hello\n",
            stderr="",
            duration_seconds=0.02,
        )

    sb.exec = _recording_exec  # type: ignore[assignment]

    result = await ws.diagnostic_exec("echo hello", timeout_seconds=3.0)
    assert result.stdout == "hello\n"
    assert result.exit_code == 0
    assert result.duration_seconds == 0.02
    assert captured["command"] == "echo hello"
    assert captured["timeout_seconds"] == 3.0
    # workdir should be the workspace root inside the sandbox
    # (default "/workspace").
    assert captured["workdir"] == "/workspace"


@pytest.mark.asyncio
async def test_diagnostic_exec_timeout_returns_minus_one(
    tmp_path: Path,
) -> None:
    """If Sandbox.exec raises TimeoutError, diagnostic_exec maps it to
    a result with exit_code=-1 rather than propagating the exception."""

    sb = FakeSandbox(root=tmp_path)
    ws = await SandboxWorkspace.materialise(
        workspace_id="ws-diag-to", template=_template(),
        sandbox=sb, backend_kind="container",
        runtime_meta=_runtime_meta(),
    )

    async def _timeout_exec(
        command, *, workdir="/workspace", env=None,
        timeout_seconds=None, stdin=None, abort=None,
    ):
        raise TimeoutError("sandbox exec timed out")

    sb.exec = _timeout_exec  # type: ignore[assignment]

    result = await ws.diagnostic_exec("sleep 99", timeout_seconds=0.1)
    assert result.exit_code == -1
    assert "timed out" in result.stderr.lower()
