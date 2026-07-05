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


class _AbsPathSandbox(FakeSandbox):
    """Mimics the real container/k8s runtime, whose ``list_dir`` returns
    ABSOLUTE entry paths (``/workspace/...``) — unlike FakeSandbox, which
    returns basenames. Reproduces the list/read path double-anchor bug so
    it can't regress."""

    async def list_dir(self, path):  # type: ignore[override]
        from primer.int.sandbox import FileStat
        base = path.rstrip("/")
        return [
            FileStat(
                path=f"{base}/{e.path}",
                kind=e.kind,
                size_bytes=e.size_bytes,
                mode=e.mode,
                modified_at=e.modified_at,
            )
            for e in await super().list_dir(path)
        ]


@pytest.mark.asyncio
async def test_list_files_roundtrip_with_absolute_runtime_paths(tmp_path: Path) -> None:
    """The real runtime returns ABSOLUTE list_dir paths; the consumer must
    still emit workspace-RELATIVE FileEntry.path that round-trips through
    read_file (bug: root files 404), and must list nested dirs (bug: the
    artifacts directory can't be expanded)."""
    sb = _AbsPathSandbox(root=tmp_path)
    ws = await SandboxWorkspace.materialise(
        workspace_id="ws-1", template=_template(),
        sandbox=sb, backend_kind="container",
        runtime_meta=_runtime_meta(),
    )
    await ws.write_file(".runtime.ready", b"ok")
    await ws.write_file("artifacts/sess/recommendation.md", b"# rec")

    # Flat root listing -> relative paths, never absolute /workspace/...
    entries = await ws.list_files(".", recursive=False)
    by_path = {e.path for e in entries}
    assert ".runtime.ready" in by_path
    assert "artifacts" in by_path
    assert not any(p.startswith("/workspace") for p in by_path)

    # Reading the LISTED path round-trips (the .runtime.ready 404 bug).
    ready = next(e for e in entries if e.path.endswith(".runtime.ready"))
    assert await ws.read_file(ready.path) == b"ok"

    # Expanding a nested directory works (the artifacts-expand bug).
    sub = await ws.list_files("artifacts", recursive=False)
    assert any(e.path == "artifacts/sess" for e in sub)


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


# ===========================================================================
# Cross-process session rehydration (parity with LocalWorkspace.get_session)
# ===========================================================================


from primer.model.workspace_session import AgentBinding


def _binding(agent_id: str = "ag-1") -> AgentBinding:
    return AgentBinding(agent_id=agent_id, agent_name="Agent One")


@pytest.mark.asyncio
async def test_get_session_rehydrates_from_persisted_state(
    tmp_path: Path,
) -> None:
    """A session created via one SandboxWorkspace wrapper is recoverable
    via a *fresh* wrapper over the same sandbox -- simulating the API
    process allocating the slot and the worker process re-attaching."""
    sb = FakeSandbox(root=tmp_path)
    ws_api = await SandboxWorkspace.materialise(
        workspace_id="ws-rh", template=_template(),
        sandbox=sb, backend_kind="container",
        runtime_meta=_runtime_meta(),
    )
    created = await ws_api.start_session(_binding(), id="sess-rh-1")
    assert created.session_id == "sess-rh-1"

    # Fresh wrapper over the SAME sandbox => empty in-memory registry, but
    # the persisted slot lives in the sandbox state repo.
    ws_worker = await SandboxWorkspace.materialise(
        workspace_id="ws-rh", template=_template(),
        sandbox=sb, backend_kind="container",
        runtime_meta=_runtime_meta(),
    )
    assert ws_worker._sessions == {}  # nothing cached yet
    rehydrated = await ws_worker.get_session("sess-rh-1")
    assert rehydrated is not None
    assert rehydrated.session_id == "sess-rh-1"
    assert rehydrated.agent_id == "ag-1"
    # Second call returns the now-cached handle.
    again = await ws_worker.get_session("sess-rh-1")
    assert again is rehydrated


@pytest.mark.asyncio
async def test_get_session_uses_in_memory_fast_path(tmp_path: Path) -> None:
    """When the slot is already cached, get_session returns it without
    touching the state repo (the same object identity)."""
    sb = FakeSandbox(root=tmp_path)
    ws = await SandboxWorkspace.materialise(
        workspace_id="ws-fast", template=_template(),
        sandbox=sb, backend_kind="container",
        runtime_meta=_runtime_meta(),
    )
    created = await ws.start_session(_binding(), id="sess-fast-1")
    got = await ws.get_session("sess-fast-1")
    assert got is created


@pytest.mark.asyncio
async def test_get_session_missing_returns_none(tmp_path: Path) -> None:
    """No persisted slot => None (mirrors LocalWorkspace.get_session)."""
    sb = FakeSandbox(root=tmp_path)
    ws = await SandboxWorkspace.materialise(
        workspace_id="ws-miss", template=_template(),
        sandbox=sb, backend_kind="container",
        runtime_meta=_runtime_meta(),
    )
    assert await ws.get_session("sess-does-not-exist") is None


@pytest.mark.asyncio
async def test_list_sessions_rehydrates_all_persisted(tmp_path: Path) -> None:
    """list_sessions surfaces every persisted session, even those created
    by a different wrapper (the API/worker split)."""
    sb = FakeSandbox(root=tmp_path)
    ws_api = await SandboxWorkspace.materialise(
        workspace_id="ws-la", template=_template(),
        sandbox=sb, backend_kind="container",
        runtime_meta=_runtime_meta(),
    )
    await ws_api.start_session(_binding("ag-a"), id="sess-la-1")
    await ws_api.start_session(_binding("ag-b"), id="sess-la-2")

    ws_worker = await SandboxWorkspace.materialise(
        workspace_id="ws-la", template=_template(),
        sandbox=sb, backend_kind="container",
        runtime_meta=_runtime_meta(),
    )
    infos = await ws_worker.list_sessions()
    ids = sorted(i.session_id for i in infos)
    assert ids == ["sess-la-1", "sess-la-2"]


@pytest.mark.asyncio
async def test_list_sessions_filters_after_rehydrate(tmp_path: Path) -> None:
    """The agent_id / status filters apply to rehydrated sessions too."""
    sb = FakeSandbox(root=tmp_path)
    ws_api = await SandboxWorkspace.materialise(
        workspace_id="ws-lf", template=_template(),
        sandbox=sb, backend_kind="container",
        runtime_meta=_runtime_meta(),
    )
    await ws_api.start_session(_binding("ag-x"), id="sess-lf-1")
    await ws_api.start_session(_binding("ag-y"), id="sess-lf-2")

    ws_worker = await SandboxWorkspace.materialise(
        workspace_id="ws-lf", template=_template(),
        sandbox=sb, backend_kind="container",
        runtime_meta=_runtime_meta(),
    )
    only_x = await ws_worker.list_sessions(agent_id="ag-x")
    assert [i.session_id for i in only_x] == ["sess-lf-1"]


@pytest.mark.asyncio
async def test_remove_session_drops_in_memory_handle(tmp_path: Path) -> None:
    """remove_session unbinds the cached handle (parity with local)."""
    sb = FakeSandbox(root=tmp_path)
    ws = await SandboxWorkspace.materialise(
        workspace_id="ws-rm", template=_template(),
        sandbox=sb, backend_kind="container",
        runtime_meta=_runtime_meta(),
    )
    await ws.start_session(_binding(), id="sess-rm-1")
    assert await ws.remove_session("sess-rm-1") is True
    assert "sess-rm-1" not in ws._sessions
    # No persisted slot was reaped in this test, so a rehydrating
    # list_sessions would re-surface it -- remove_session only unbinds the
    # cache, exactly like LocalWorkspace.remove_session.
    assert await ws.remove_session("never-existed") is False


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
