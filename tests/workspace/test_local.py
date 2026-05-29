"""Tests for primer.workspace.local.LocalWorkspaceBackend + LocalWorkspace."""

from __future__ import annotations

import io
import shutil
import sys
import tarfile
from pathlib import Path

import pytest

from primer.model.except_ import BadRequestError, NotFoundError
from primer.model.workspace_session import AgentBinding, SessionStatus
from primer.model.workspace import (
    FileMount,
    ResourceLimits,
    WorkspaceTemplate,
    WorkspaceTemplateOverrides,
)
from primer.workspace import LocalWorkspace, LocalWorkspaceBackend


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git CLI not available on PATH (StateRepo needs it)",
)


# ===========================================================================
# Helpers
# ===========================================================================


def _template(
    *,
    files: list[FileMount] | None = None,
    init_commands: list[str] | None = None,
    env: dict[str, str] | None = None,
    resources: ResourceLimits | None = None,
    provider_id: str = "local-1",
) -> WorkspaceTemplate:
    return WorkspaceTemplate(
        id="dev",
        description="local dev template",
        provider_id=provider_id,
        files=files or [],
        init_commands=init_commands or [],
        env={k: v for k, v in (env or {}).items()},
        resources=resources or ResourceLimits(),
    )


def _binding(
    *, agent_id: str = "agent-foo", name: str = "Agent Foo"
) -> AgentBinding:
    return AgentBinding(agent_id=agent_id, agent_name=name)


@pytest.fixture
async def provider(tmp_path: Path) -> LocalWorkspaceBackend:
    p = LocalWorkspaceBackend(tmp_path / "provider_root")
    await p.initialize()
    return p


# ===========================================================================
# LocalWorkspaceBackend — lifecycle
# ===========================================================================


class TestProviderLifecycle:
    async def test_initialize_creates_root(self, tmp_path: Path) -> None:
        p = LocalWorkspaceBackend(tmp_path / "provider_root")
        assert not (tmp_path / "provider_root").exists()
        await p.initialize()
        assert (tmp_path / "provider_root").is_dir()

    async def test_initialize_is_idempotent(
        self, tmp_path: Path
    ) -> None:
        p = LocalWorkspaceBackend(tmp_path / "provider_root")
        await p.initialize()
        await p.initialize()
        assert (tmp_path / "provider_root").is_dir()

    async def test_aclose_clears_registry(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        await provider.create(_template())
        assert len(await provider.list()) == 1
        await provider.aclose()
        assert await provider.list() == []

    async def test_root_property(self, tmp_path: Path) -> None:
        p = LocalWorkspaceBackend(tmp_path / "p")
        assert p.root == tmp_path / "p"


# ===========================================================================
# LocalWorkspaceBackend — create()
# ===========================================================================


class TestProviderCreate:
    async def test_creates_workspace_with_unique_id(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws1 = await provider.create(_template())
        ws2 = await provider.create(_template())
        assert ws1.id != ws2.id
        assert ws1.id.startswith("ws-")

    async def test_creates_root_directory(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws = await provider.create(_template())
        assert isinstance(ws, LocalWorkspace)
        assert ws.root.is_dir()
        assert ws.root.name == ws.id

    async def test_initializes_state_repo(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws = await provider.create(_template())
        assert isinstance(ws, LocalWorkspace)
        assert (ws.root / ".state" / ".git").is_dir()

    async def test_creates_tmp_directory(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws = await provider.create(_template())
        assert isinstance(ws, LocalWorkspace)
        assert (ws.root / ".tmp").is_dir()

    async def test_provides_seven_tools(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws = await provider.create(_template())
        ids = {t.id for t in ws.get_tools()}
        assert ids == {"ls", "read", "write", "edit", "glob", "grep", "exec"}

    async def test_materialises_inline_files(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        tpl = _template(
            files=[
                FileMount(
                    path="README.md",
                    source={"kind": "inline", "content": "# Hello"},
                ),
                FileMount(
                    path="config/main.yaml",
                    source={"kind": "inline", "content": "key: value\n"},
                ),
            ]
        )
        ws = await provider.create(tpl)
        assert isinstance(ws, LocalWorkspace)
        assert (ws.root / "README.md").read_text(encoding="utf-8") == "# Hello"
        assert (ws.root / "config" / "main.yaml").read_text(encoding="utf-8") == (
            "key: value\n"
        )

    async def test_runs_init_commands(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        tpl = _template(
            init_commands=[
                f'"{sys.executable}" -c '
                f'"open(\'marker.txt\', \'w\').write(\'init ran\')"'
            ]
        )
        ws = await provider.create(tpl)
        assert isinstance(ws, LocalWorkspace)
        assert (ws.root / "marker.txt").read_text() == "init ran"

    async def test_init_command_failure_rolls_back(
        self, provider: LocalWorkspaceBackend, tmp_path: Path
    ) -> None:
        tpl = _template(
            init_commands=[f'"{sys.executable}" -c "import sys; sys.exit(7)"']
        )
        with pytest.raises(BadRequestError, match="init command failed"):
            await provider.create(tpl)
        # The partially-built workspace dir should have been removed.
        provider_root = tmp_path / "provider_root"
        children = list(provider_root.iterdir())
        assert children == []

    async def test_unknown_file_source_logs_warning_and_skips(
        self,
        provider: LocalWorkspaceBackend,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        tpl = _template(
            files=[
                FileMount(
                    path="foo",
                    source={"kind": "url", "url": "https://example.test/foo"},
                )
            ]
        )
        with caplog.at_level("WARNING"):
            ws = await provider.create(tpl)
        assert isinstance(ws, LocalWorkspace)
        assert not (ws.root / "foo").exists()
        assert any(
            "file source kind not yet supported" in record.message
            for record in caplog.records
        )

    async def test_warns_on_resource_limits(
        self,
        provider: LocalWorkspaceBackend,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        tpl = _template(resources=ResourceLimits(cpu_cores=4))
        with caplog.at_level("WARNING"):
            await provider.create(tpl)
        assert any("resource limits" in r.message for r in caplog.records)

    async def test_overrides_extend_files(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        tpl = _template(
            files=[
                FileMount(
                    path="base.txt",
                    source={"kind": "inline", "content": "base"},
                )
            ]
        )
        overrides = WorkspaceTemplateOverrides(
            files=[
                FileMount(
                    path="extra.txt",
                    source={"kind": "inline", "content": "extra"},
                )
            ],
        )
        ws = await provider.create(tpl, overrides=overrides)
        assert isinstance(ws, LocalWorkspace)
        assert (ws.root / "base.txt").read_text() == "base"
        assert (ws.root / "extra.txt").read_text() == "extra"

    async def test_overrides_extend_init_commands(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        tpl = _template(
            init_commands=[
                f'"{sys.executable}" -c '
                f'"open(\'a.txt\',\'w\').write(\'A\')"'
            ]
        )
        overrides = WorkspaceTemplateOverrides(
            init_commands=[
                f'"{sys.executable}" -c '
                f'"open(\'b.txt\',\'w\').write(\'B\')"'
            ],
        )
        ws = await provider.create(tpl, overrides=overrides)
        assert isinstance(ws, LocalWorkspace)
        assert (ws.root / "a.txt").read_text() == "A"
        assert (ws.root / "b.txt").read_text() == "B"


# ===========================================================================
# LocalWorkspaceBackend — get/list/destroy
# ===========================================================================


class TestProviderGetListDestroy:
    async def test_get_returns_workspace(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws = await provider.create(_template())
        same = await provider.get(ws.id)
        assert same is ws

    async def test_get_returns_none_for_missing(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        assert await provider.get("nope") is None

    async def test_list_returns_ids(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        a = await provider.create(_template())
        b = await provider.create(_template())
        ids = await provider.list()
        assert set(ids) == {a.id, b.id}

    async def test_destroy_removes_workspace(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws = await provider.create(_template())
        assert isinstance(ws, LocalWorkspace)
        root = ws.root
        await provider.destroy(ws.id)
        assert ws.id not in await provider.list()
        assert not root.exists()

    async def test_destroy_unknown_raises(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        with pytest.raises(NotFoundError):
            await provider.destroy("ws-nope")


# ===========================================================================
# LocalWorkspace — sessions
# ===========================================================================


class TestWorkspaceSessions:
    async def test_start_session_returns_running(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws = await provider.create(_template())
        session = await ws.start_session(_binding())
        assert await session.status() == SessionStatus.RUNNING
        assert session.workspace_id == ws.id
        assert session.agent_id == "agent-foo"

    async def test_start_session_attaches_workspace_tools(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws = await provider.create(_template())
        session = await ws.start_session(_binding())
        ids = {t.id for t in session.workspace_tools}
        assert ids == {"ls", "read", "write", "edit", "glob", "grep", "exec"}

    async def test_list_sessions_filter_by_agent(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws = await provider.create(_template())
        await ws.start_session(_binding(agent_id="a"))
        await ws.start_session(_binding(agent_id="b"))
        out = await ws.list_sessions(agent_id="a")
        assert len(out) == 1
        assert out[0].agent_id == "a"

    async def test_list_sessions_filter_by_status(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws = await provider.create(_template())
        s1 = await ws.start_session(_binding())
        s2 = await ws.start_session(_binding())
        await s1.aclose()
        del s2
        running = await ws.list_sessions(status=SessionStatus.RUNNING)
        ended = await ws.list_sessions(status=SessionStatus.ENDED)
        assert len(running) == 1
        assert len(ended) == 1

    async def test_list_sessions_newest_first(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws = await provider.create(_template())
        s1 = await ws.start_session(_binding())
        s2 = await ws.start_session(_binding())
        out = await ws.list_sessions()
        assert [i.session_id for i in out][:2] == [s2.session_id, s1.session_id]

    async def test_get_session(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws = await provider.create(_template())
        s = await ws.start_session(_binding())
        same = await ws.get_session(s.session_id)
        assert same is s
        assert await ws.get_session("nope") is None

    async def test_start_session_with_explicit_id(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        """When `id` is supplied, the session should use it instead of
        generating a fresh UUID. Lets the REST API pre-allocate the id."""
        ws = await provider.create(_template())
        session = await ws.start_session(_binding(), id="sess-explicit-1")
        assert session.session_id == "sess-explicit-1"

    async def test_start_session_with_duplicate_id_raises(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        from primer.model.except_ import ConflictError

        ws = await provider.create(_template())
        await ws.start_session(_binding(), id="dup")
        with pytest.raises(ConflictError):
            await ws.start_session(_binding(), id="dup")


# ===========================================================================
# LocalWorkspace — file browsing
# ===========================================================================


class TestWorkspaceFiles:
    async def test_list_files_root(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        tpl = _template(
            files=[
                FileMount(
                    path="a.txt", source={"kind": "inline", "content": "a"}
                ),
                FileMount(
                    path="b.txt", source={"kind": "inline", "content": "bb"}
                ),
            ]
        )
        ws = await provider.create(tpl)
        entries = await ws.list_files(".")
        names = {e.path for e in entries}
        assert "a.txt" in names
        assert "b.txt" in names

    async def test_list_files_recursive(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        tpl = _template(
            files=[
                FileMount(
                    path="src/main.py",
                    source={"kind": "inline", "content": "pass"},
                ),
            ]
        )
        ws = await provider.create(tpl)
        entries = await ws.list_files(".", recursive=True)
        rels = {e.path for e in entries}
        assert "src/main.py" in rels

    async def test_list_files_missing(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws = await provider.create(_template())
        with pytest.raises(NotFoundError):
            await ws.list_files("nope")

    async def test_list_files_rejects_escape(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws = await provider.create(_template())
        with pytest.raises(BadRequestError, match="outside workspace"):
            await ws.list_files("../..")

    async def test_read_file(self, provider: LocalWorkspaceBackend) -> None:
        tpl = _template(
            files=[
                FileMount(
                    path="hello.txt",
                    source={"kind": "inline", "content": "hello world"},
                )
            ]
        )
        ws = await provider.create(tpl)
        data = await ws.read_file("hello.txt")
        assert data == b"hello world"

    async def test_read_file_missing(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws = await provider.create(_template())
        with pytest.raises(NotFoundError):
            await ws.read_file("nope")

    async def test_read_file_rejects_directory(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws = await provider.create(_template())
        assert isinstance(ws, LocalWorkspace)
        (ws.root / "subdir").mkdir()
        with pytest.raises(BadRequestError, match="not a file"):
            await ws.read_file("subdir")


# ===========================================================================
# LocalWorkspace — download_archive
# ===========================================================================


class TestWorkspaceDownloadArchive:
    async def test_default_excludes_state_and_tmp(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        tpl = _template(
            files=[
                FileMount(
                    path="kept.txt", source={"kind": "inline", "content": "keep"}
                )
            ]
        )
        ws = await provider.create(tpl)
        # Start a session so .state/sessions/... and .tmp/<sid>/ exist.
        await ws.start_session(_binding())
        chunks = bytearray()
        async for chunk in ws.download_archive():
            chunks.extend(chunk)
        with tarfile.open(fileobj=io.BytesIO(bytes(chunks)), mode="r") as tf:
            names = tf.getnames()
        assert "kept.txt" in names
        # Verify no .state or .tmp top-level entries leaked in.
        assert not any(n.startswith(".state") for n in names)
        assert not any(n.startswith(".tmp") for n in names)

    async def test_explicit_paths(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        tpl = _template(
            files=[
                FileMount(
                    path="a.txt", source={"kind": "inline", "content": "A"}
                ),
                FileMount(
                    path="b.txt", source={"kind": "inline", "content": "B"}
                ),
            ]
        )
        ws = await provider.create(tpl)
        chunks = bytearray()
        async for chunk in ws.download_archive(paths=["a.txt"]):
            chunks.extend(chunk)
        with tarfile.open(fileobj=io.BytesIO(bytes(chunks)), mode="r") as tf:
            names = tf.getnames()
        assert "a.txt" in names
        assert "b.txt" not in names

    async def test_explicit_path_missing(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws = await provider.create(_template())
        with pytest.raises(NotFoundError):
            async for _ in ws.download_archive(paths=["nope.txt"]):
                pass


# ===========================================================================
# LocalWorkspace — aclose
# ===========================================================================


class TestWorkspaceAclose:
    async def test_aclose_ends_running_sessions(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws = await provider.create(_template())
        s = await ws.start_session(_binding())
        await ws.aclose()
        assert await s.status() == SessionStatus.ENDED

    async def test_aclose_idempotent_via_destroy(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws = await provider.create(_template())
        await ws.start_session(_binding())
        # destroy() invokes aclose internally.
        await provider.destroy(ws.id)


# ===========================================================================
# WorkspaceBackendFactory
# ===========================================================================


class TestFactory:
    async def test_create_local_backend_from_config(self, tmp_path: Path) -> None:
        from primer.model.workspace import (
            LocalWorkspaceConfig,
            WorkspaceProvider,
            WorkspaceProviderType,
        )
        from primer.workspace import WorkspaceBackendFactory

        config = WorkspaceProvider(
            id="local-1",
            provider=WorkspaceProviderType.LOCAL,
            config=LocalWorkspaceConfig(path=str(tmp_path / "factory_root")),
        )
        backend = WorkspaceBackendFactory.create(config)
        assert isinstance(backend, LocalWorkspaceBackend)
        await backend.initialize()
        # Backend should materialise workspaces under the configured path.
        ws = await backend.create(_template())
        assert (tmp_path / "factory_root" / ws.id).is_dir()
        await backend.aclose()


# ===========================================================================
# LocalWorkspace.append_message_line
# ===========================================================================


class TestAppendMessageLine:
    """append_message_line writes session records to the right path."""

    async def test_creates_messages_jsonl_on_first_call(
        self, provider: LocalWorkspaceBackend, tmp_path: Path
    ) -> None:
        ws = await provider.create(_template())
        sid = "sess-aml-1"
        await ws.append_message_line(sid, b'{"seq":1,"kind":"done"}\n')

        # Path: <root>/<state_path>/sessions/<sid>/messages.jsonl
        expected = ws.root / ws.template.state_path / "sessions" / sid / "messages.jsonl"
        assert expected.exists()
        assert expected.read_bytes() == b'{"seq":1,"kind":"done"}\n'

    async def test_appends_across_multiple_calls(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws = await provider.create(_template())
        sid = "sess-aml-2"
        line1 = b'{"seq":1,"kind":"user_input"}\n'
        line2 = b'{"seq":2,"kind":"assistant_token"}\n'
        line3 = b'{"seq":3,"kind":"done"}\n'

        await ws.append_message_line(sid, line1)
        await ws.append_message_line(sid, line2)
        await ws.append_message_line(sid, line3)

        path = ws.root / ws.template.state_path / "sessions" / sid / "messages.jsonl"
        content = path.read_bytes()
        assert content == line1 + line2 + line3

    async def test_ensures_trailing_newline(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws = await provider.create(_template())
        sid = "sess-aml-3"
        # Pass a line WITHOUT a trailing newline — the method must add one.
        await ws.append_message_line(sid, b'{"seq":1,"kind":"done"}')

        path = ws.root / ws.template.state_path / "sessions" / sid / "messages.jsonl"
        content = path.read_bytes()
        assert content.endswith(b"\n")

    async def test_no_op_for_empty_line(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws = await provider.create(_template())
        sid = "sess-aml-4"
        # Appending empty bytes should be a no-op; no file created.
        await ws.append_message_line(sid, b"")
        path = ws.root / ws.template.state_path / "sessions" / sid / "messages.jsonl"
        assert not path.exists()

    async def test_batched_flush_appends_all_lines(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        """Simulate WorkspaceMessageWriter flushing multiple lines at once."""
        ws = await provider.create(_template())
        sid = "sess-aml-5"
        batch = b'{"seq":1}\n{"seq":2}\n{"seq":3}\n'
        await ws.append_message_line(sid, batch)

        path = ws.root / ws.template.state_path / "sessions" / sid / "messages.jsonl"
        assert path.read_bytes() == batch
