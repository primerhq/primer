"""Tests for primer.workspace.local.LocalWorkspaceBackend + LocalWorkspace."""

from __future__ import annotations

import asyncio
import io
import shutil
import sys
import tarfile
from pathlib import Path

import pytest

from primer.model.except_ import BadRequestError, ConflictError, NotFoundError
from primer.model.workspace_session import AgentBinding, SessionStatus
from primer.model.workspace import (
    FileMount,
    ResourceLimits,
    WorkspaceTemplate,
    WorkspaceTemplateOverrides,
)
from primer.workspace import LocalWorkspace, LocalWorkspaceBackend
from primer.workspace.tool import ToolCallContext


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


async def _materialise_local(
    tmp_path: Path, *, strict: bool = False
) -> LocalWorkspace:
    """Build a LocalWorkspace directly (no provider) with the lock table wired.

    ``strict`` opts the template into whole-root scope locking so the
    write/exec Tier-A/Tier-B scope keys collapse to the workspace root.
    """
    root = tmp_path / "ws-locks-root"
    root.mkdir(exist_ok=True)
    tpl = _template().model_copy(update={"strict_write_locking": strict})
    return await LocalWorkspace.materialise(
        workspace_id="ws-locks-1", root=root, template=tpl, env={},
    )


def tmp_path_root(ws: LocalWorkspace) -> Path:
    """The on-disk root a materialised local workspace writes under."""
    return ws.root


async def _call_tool(session, tool_id: str, args: dict):
    """Validate ``args`` and run one workspace tool through a session context.

    Mirrors how the runtime dispatches a tool call: it looks the tool up
    on the session, validates the raw args against the tool's Pydantic
    schema, then executes it with a minimal :class:`ToolCallContext`.
    """
    tool = next(t for t in session.workspace_tools if t.id == tool_id)
    validated = tool.parameters().model_validate(args)
    ctx = ToolCallContext(
        workspace_id=session.workspace_id,
        session_id=session.session_id,
        agent_id=session.agent_id,
        call_id="call-test",
        abort=asyncio.Event(),
        session=session,
    )
    return await tool.execute(validated, ctx)


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

    async def test_url_file_source_is_fetched_and_written(
        self,
        provider: LocalWorkspaceBackend,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A kind=url FileMount is fetched via the central resolver and
        the resulting bytes land in the workspace fs."""

        class _FakeResp:
            status = 200

            async def read(self) -> bytes:
                return b"remote-bytes"

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

        class _FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            def get(self, url, **_):
                return _FakeResp()

        monkeypatch.setattr(
            "primer.workspace.files._http_session", lambda: _FakeSession()
        )
        tpl = _template(
            files=[
                FileMount(
                    path="foo",
                    source={"kind": "url", "url": "https://example.test/foo"},
                )
            ]
        )
        ws = await provider.create(tpl)
        assert isinstance(ws, LocalWorkspace)
        assert (ws.root / "foo").read_bytes() == b"remote-bytes"

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

    async def test_remove_session_reaps_on_disk_slot(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        """remove_session reaps the on-disk slot so a rehydrating
        get_session no longer resurrects the deleted session.

        Regression: the reap used to live only in the API delete handler
        (a host rmtree), so remove_session alone left the persisted slot
        under ``.state/sessions/<sid>/`` on disk and get_session rebuilt
        the handle straight back from it.
        """
        ws = await provider.create(_template())
        await ws.start_session(_binding(), id="sess-reap-local")
        slot = ws.state_repo.path / "sessions" / "sess-reap-local"
        assert slot.exists()

        assert await ws.remove_session("sess-reap-local") is True

        # Slot reaped on disk, so the rehydrating get_session returns None.
        assert not slot.exists()
        assert await ws.get_session("sess-reap-local") is None

    async def test_get_session_heals_stale_cached_status_from_disk(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        """A cached handle whose in-memory status went stale (the turn ran
        through a different process / workspace-cache instance that committed
        ENDED to ``session.json``) is re-synced from disk on ``get_session``.

        Regression for the MCP ``get_workspace_session`` /
        ``list_workspace_sessions`` "session stuck running forever" bug: the
        worker writes ENDED to disk but the API process's cached handle held
        a RUNNING snapshot, so the workspace tools reported a terminated
        session as still running.
        """
        ws = await provider.create(_template())
        session = await ws.start_session(_binding(), id="sess-heal-1")
        # Commit ENDED to disk (this is what the worker's dispatch terminal
        # transition does), then forcibly revert the in-memory snapshot to
        # RUNNING to simulate a cache that missed the cross-process update.
        await session.set_status(SessionStatus.ENDED, ended_reason="completed")
        from primer.model.workspace_session import SessionStatus as _S
        session._info = session._info.model_copy(update={"status": _S.RUNNING})
        assert await session.status() == SessionStatus.RUNNING  # stale

        healed = await ws.get_session("sess-heal-1")
        assert healed is session
        assert await healed.status() == SessionStatus.ENDED
        assert (await healed.info()).ended_reason == "completed"

    async def test_list_sessions_heals_stale_cached_status_from_disk(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        """``list_sessions`` also re-syncs each cached handle from disk so a
        worker-ended session isn't reported as RUNNING in the list view."""
        ws = await provider.create(_template())
        session = await ws.start_session(_binding(), id="sess-heal-2")
        await session.set_status(SessionStatus.ENDED, ended_reason="cancelled")
        from primer.model.workspace_session import SessionStatus as _S
        session._info = session._info.model_copy(update={"status": _S.RUNNING})

        ended = await ws.list_sessions(status=SessionStatus.ENDED)
        assert [i.session_id for i in ended] == ["sess-heal-2"]
        running = await ws.list_sessions(status=SessionStatus.RUNNING)
        assert running == []


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

    async def test_make_dir(self, provider: LocalWorkspaceBackend) -> None:
        ws = await provider.create(_template())
        await ws.make_dir("src/nested")
        info = await ws.file_info("src/nested")
        assert info.kind == "dir"

    async def test_make_dir_conflict(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws = await provider.create(_template())
        await ws.make_dir("src")
        with pytest.raises(BadRequestError, match="already exists"):
            await ws.make_dir("src")

    async def test_make_dir_rejects_reserved(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws = await provider.create(_template())
        with pytest.raises(BadRequestError):
            await ws.make_dir(".state/sneaky")

    async def test_delete_empty_dir(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws = await provider.create(_template())
        await ws.make_dir("empty")
        await ws.delete_file("empty")
        with pytest.raises(NotFoundError):
            await ws.file_info("empty")

    async def test_delete_nonempty_dir_refused(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws = await provider.create(_template())
        await ws.make_dir("d")
        await ws.write_file("d/a.txt", b"x")
        with pytest.raises(BadRequestError, match="not empty"):
            await ws.delete_file("d")

    async def test_delete_dir_recursive(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws = await provider.create(_template())
        await ws.make_dir("d")
        await ws.write_file("d/a.txt", b"x")
        await ws.delete_file("d", recursive=True)
        with pytest.raises(NotFoundError):
            await ws.file_info("d")

    async def test_move_renames_file(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws = await provider.create(_template())
        await ws.write_file("a.txt", b"hello")
        await ws.move_file("a.txt", "b.txt")
        assert await ws.read_file("b.txt") == b"hello"
        with pytest.raises(NotFoundError):
            await ws.file_info("a.txt")

    async def test_move_file_into_subdir(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws = await provider.create(_template())
        await ws.write_file("note.md", b"# n")
        # Parent dir is created on demand by move.
        await ws.move_file("note.md", "docs/note.md")
        assert await ws.read_file("docs/note.md") == b"# n"

    async def test_move_renames_dir_with_children(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws = await provider.create(_template())
        await ws.make_dir("old")
        await ws.write_file("old/a.txt", b"a")
        await ws.move_file("old", "new")
        assert await ws.read_file("new/a.txt") == b"a"
        with pytest.raises(NotFoundError):
            await ws.file_info("old")

    async def test_move_missing_src_raises(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws = await provider.create(_template())
        with pytest.raises(NotFoundError):
            await ws.move_file("nope.txt", "there.txt")

    async def test_move_onto_existing_dst_conflicts(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws = await provider.create(_template())
        await ws.write_file("a.txt", b"a")
        await ws.write_file("b.txt", b"b")
        with pytest.raises(ConflictError):
            await ws.move_file("a.txt", "b.txt")
        # The source is untouched by a rejected move.
        assert await ws.read_file("a.txt") == b"a"

    async def test_move_rejects_escape(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws = await provider.create(_template())
        await ws.write_file("a.txt", b"a")
        with pytest.raises(BadRequestError, match="outside workspace"):
            await ws.move_file("a.txt", "../escape.txt")

    async def test_move_rejects_reserved_dst(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws = await provider.create(_template())
        await ws.write_file("a.txt", b"a")
        with pytest.raises(BadRequestError, match="reserved"):
            await ws.move_file("a.txt", ".state/a.txt")

    async def test_move_dir_into_own_descendant_rejected(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws = await provider.create(_template())
        await ws.make_dir("src")
        with pytest.raises(BadRequestError, match="itself or a"):
            await ws.move_file("src", "src/inner")

    async def test_write_file_is_atomic_no_torn_read(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        """Regression for e2e t0605: a write racing concurrent reads of
        the same path must never expose a torn/empty file. With an
        atomic write (temp file + ``os.replace``) every read observes
        either the full old content or the full new content.

        We hammer the path: many overwrites alternating between two
        distinct-length blobs while many readers race them. Every read
        must return one of the two complete snapshots, never an empty
        or partial buffer.
        """
        import asyncio

        ws = await provider.create(_template())
        assert isinstance(ws, LocalWorkspace)

        pre = b"pre"
        post = b"post-content-which-is-longer"
        await ws.write_file("race.txt", pre)

        allowed = {pre, post}
        torn: list[bytes] = []

        async def _writer() -> None:
            for i in range(50):
                await ws.write_file("race.txt", post if i % 2 else pre)

        async def _reader() -> None:
            for _ in range(200):
                data = await ws.read_file("race.txt")
                if data not in allowed:
                    torn.append(data)

        await asyncio.gather(
            _writer(),
            *[_reader() for _ in range(8)],
        )

        assert not torn, f"observed torn/empty reads: {torn[:5]!r}"

    async def test_write_file_preserves_mode(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        """The atomic swap must preserve an existing file's mode rather
        than adopting the temp file's default permissions."""
        import os
        import sys

        ws = await provider.create(_template())
        assert isinstance(ws, LocalWorkspace)

        await ws.write_file("perm.txt", b"first")
        target = ws.root / "perm.txt"
        os.chmod(target, 0o640)
        await ws.write_file("perm.txt", b"second")

        assert await ws.read_file("perm.txt") == b"second"
        if sys.platform != "win32":
            assert (target.stat().st_mode & 0o777) == 0o640


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
            config=LocalWorkspaceConfig(root_path=str(tmp_path / "factory_root")),
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


class TestDiagnosticExec:
    """LocalWorkspace.diagnostic_exec runs a shell command rooted at the
    workspace path and returns stdout/stderr/exit_code/duration."""

    async def test_echo_returns_stdout(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws = await provider.create(_template())
        assert isinstance(ws, LocalWorkspace)
        result = await ws.diagnostic_exec("echo hello")
        assert result.exit_code == 0
        assert result.stdout == "hello\n"
        assert result.stderr == ""
        assert result.duration_seconds >= 0.0

    async def test_pwd_runs_in_workspace_root(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws = await provider.create(_template())
        assert isinstance(ws, LocalWorkspace)
        result = await ws.diagnostic_exec("pwd")
        assert result.exit_code == 0
        # `pwd` prints the cwd; the workspace root is the cwd. Use
        # resolve() to handle macOS /tmp -> /private/tmp symlinks.
        assert result.stdout.strip() == str(ws.root.resolve())

    async def test_nonzero_exit_propagates(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws = await provider.create(_template())
        assert isinstance(ws, LocalWorkspace)
        # `ls` on a missing path exits non-zero on POSIX.
        result = await ws.diagnostic_exec("ls definitely-not-a-real-path-xyz")
        assert result.exit_code != 0
        assert result.stderr != ""

    async def test_timeout_kills_process(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws = await provider.create(_template())
        assert isinstance(ws, LocalWorkspace)
        # `sleep 5` should be killed at 0.2s. `sleep` isn't on the
        # whitelist (the route filters that) but diagnostic_exec runs
        # whatever it's told — timeout enforcement is the contract we
        # care about here.
        result = await ws.diagnostic_exec("sleep 5", timeout_seconds=0.2)
        assert result.exit_code == -1
        assert result.duration_seconds < 5.0


# ===========================================================================
# LocalWorkspaceBackend — re-attach after process restart
# ===========================================================================


class TestReAttachAfterRestart:
    async def test_get_returns_none_when_dir_missing(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        out = await provider.get("ws-does-not-exist", template=_template())
        assert out is None

    async def test_get_returns_none_without_template_for_uncached(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        """Re-attach needs a template; without it the backend returns
        None rather than guessing."""
        ws = await provider.create(_template())
        wid = ws.id
        # Simulate a "fresh process" by dropping the in-memory cache.
        await provider.aclose()
        provider2 = LocalWorkspaceBackend(provider.root)
        await provider2.initialize()
        out = await provider2.get(wid, template=None)
        assert out is None

    async def test_get_reattaches_existing_workspace_on_fresh_process(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        """The on-disk directory survives the process; the second
        backend instance MUST rebuild a LocalWorkspace from it.

        This is the fix for the diagnostic-report Bug 2 — pre-fix the
        local backend returned None for any workspace not materialised
        by the current process, producing the
        'row exists but the backend has no live instance and re-attach
        failed' error in the workspace registry.
        """
        tpl = _template()
        ws1 = await provider.create(tpl)
        wid = ws1.id
        assert (provider.root / wid).is_dir()

        # Simulate a process restart — drop the in-memory cache.
        await provider.aclose()
        provider2 = LocalWorkspaceBackend(provider.root)
        await provider2.initialize()

        # Re-attach via get() — must NOT return None now.
        ws2 = await provider2.get(wid, template=tpl)
        assert ws2 is not None
        assert ws2.id == wid
        # The re-attached workspace works just like the original.
        result = await ws2.diagnostic_exec("pwd")
        assert result.exit_code == 0


# ===========================================================================
# Cross-process session rehydration (distributed worker support)
# ===========================================================================


class TestCrossProcessRehydration:
    """A session created on one process must be runnable on another.

    The API process allocates the slot via ``start_session`` (writing
    ``.state/sessions/<sid>/session.json`` + ``agent.json`` to shared
    disk); a separate worker process re-attaches the workspace with an
    empty in-memory registry and must rebuild the session handle from
    disk. Before this was supported, ``get_session`` returned None and
    the worker failed to build the executor (blocking SMK-DST-06).
    """

    async def test_get_session_rehydrates_slot_from_another_instance(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "ws-xproc"
        root.mkdir()
        tpl = _template()
        # Instance A: the API process. Allocate the slot.
        ws_a = await LocalWorkspace.materialise(
            workspace_id="ws-xproc", root=root, template=tpl, env={},
        )
        created = await ws_a.start_session(
            _binding(agent_id="agent-x"), id="sess-xproc-1"
        )
        assert created.session_id == "sess-xproc-1"

        # Instance B: a worker process. Fresh in-memory registry, same
        # on-disk root. get_session must rehydrate the slot from disk.
        ws_b = await LocalWorkspace.materialise(
            workspace_id="ws-xproc", root=root, template=tpl, env={},
        )
        rehydrated = await ws_b.get_session("sess-xproc-1")
        assert rehydrated is not None
        assert rehydrated.session_id == "sess-xproc-1"
        assert rehydrated.agent_id == "agent-x"
        assert (await rehydrated.status()) == SessionStatus.RUNNING

        # Idempotent: the rehydrated handle is cached, not rebuilt.
        again = await ws_b.get_session("sess-xproc-1")
        assert again is rehydrated

    async def test_get_session_returns_none_for_unknown_slot(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "ws-xproc2"
        root.mkdir()
        ws = await LocalWorkspace.materialise(
            workspace_id="ws-xproc2", root=root, template=_template(), env={},
        )
        assert await ws.get_session("sess-does-not-exist") is None


# ===========================================================================
# LocalWorkspace - Tier-A/Tier-B write-lock wiring (workspace-file-safety)
# ===========================================================================


class TestLocalWriteLocking:
    """The local backend + its tools acquire the write-lock table and the
    write/edit tools become atomic (temp file + os.replace)."""

    async def test_two_writes_same_file_do_not_corrupt(
        self, tmp_path: Path
    ) -> None:
        """Two concurrent write tool calls at the same path must leave the
        file as exactly one of the two full payloads, never a torn
        interleave of both."""
        ws = await _materialise_local(tmp_path)
        sess = await ws.start_session(_binding())
        big_a, big_b = "A" * 200_000, "B" * 200_000
        await asyncio.gather(
            _call_tool(sess, "write", {"path": "f.txt", "content": big_a}),
            _call_tool(
                sess, "write", {"path": "f.txt", "content": big_b, "force": True}
            ),
        )
        final = (tmp_path_root(ws) / "f.txt").read_text()
        assert final in (big_a, big_b)
        assert set(final) in ({"A"}, {"B"})  # no interleave

    async def test_write_tool_is_atomic_against_reader(
        self, tmp_path: Path
    ) -> None:
        """A concurrent reader of a path being overwritten by the write tool
        must only ever observe the full old or full new content, never a
        truncated / partially-written buffer."""
        ws = await _materialise_local(tmp_path)
        sess = await ws.start_session(_binding())
        root = tmp_path_root(ws)
        (root / "g.txt").write_text("OLD")
        new = "N" * 500_000

        async def writer() -> None:
            await _call_tool(
                sess, "write", {"path": "g.txt", "content": new, "force": True}
            )

        async def reader() -> None:
            seen: set[str] = set()
            for _ in range(60):
                try:
                    seen.add((root / "g.txt").read_text())
                except FileNotFoundError:
                    pass
                await asyncio.sleep(0)
            assert seen <= {"OLD", new}

        await asyncio.gather(writer(), reader())

    async def test_edit_tool_is_atomic_against_reader(
        self, tmp_path: Path
    ) -> None:
        """The edit tool's read-modify-write is atomic under the Tier-A lock:
        a racing reader never sees a torn buffer."""
        ws = await _materialise_local(tmp_path)
        sess = await ws.start_session(_binding())
        root = tmp_path_root(ws)
        old = "X" * 200_000
        (root / "e.txt").write_text(old)
        new = "Y" * 200_000

        async def editor() -> None:
            await _call_tool(
                sess,
                "edit",
                {"path": "e.txt", "old_string": old, "new_string": new},
            )

        async def reader() -> None:
            seen: set[str] = set()
            for _ in range(60):
                try:
                    seen.add((root / "e.txt").read_text())
                except FileNotFoundError:
                    pass
                await asyncio.sleep(0)
            assert seen <= {old, new}

        await asyncio.gather(editor(), reader())

    async def test_move_into_dir_serializes_with_same_dir_write(
        self, tmp_path: Path
    ) -> None:
        """A move whose DESTINATION dir is D serializes against a write to a
        file in D (both are Tier-A writers on scope[D]) -- neither observes a
        half-state: both complete, dst is present, the other file is intact,
        and the source is gone."""
        ws = await _materialise_local(tmp_path)
        root = tmp_path_root(ws)
        (root / "src.txt").write_text("MOVED")
        (root / "sub").mkdir()
        await asyncio.gather(
            ws.move_file("src.txt", "sub/dst.txt"),
            ws.write_file("sub/other.txt", b"OTHER"),
        )
        assert (root / "sub" / "dst.txt").read_text() == "MOVED"
        assert (root / "sub" / "other.txt").read_bytes() == b"OTHER"
        assert not (root / "src.txt").exists()

    @pytest.mark.skipif(
        sys.platform == "win32", reason="POSIX shell (sleep) required"
    )
    async def test_same_dir_write_and_write_exec_serialize(
        self, tmp_path: Path
    ) -> None:
        """A write-exec (Tier-B scope lock on the workdir) and a tool write
        (Tier-A scope+path lock) in the SAME directory MUST serialize on the
        shared scope lock.

        This is the load-bearing scope-key-consistency guarantee: the exec
        holds the ``sub/`` scope for ~0.5s while a concurrent tool write to
        ``sub/w.txt`` is fired after a short head start. If exec and write
        derived the scope key the same way, the write parks on the busy scope
        lock and only completes AFTER the exec releases -> order == [exec,
        write]. If the derivations diverged, the fast write would slip in
        during the exec's sleep and the order would flip.
        """
        ws = await _materialise_local(tmp_path)
        sess = await ws.start_session(_binding())
        root = tmp_path_root(ws)
        (root / "sub").mkdir()

        order: list[str] = []

        async def exec_holder() -> None:
            await _call_tool(
                sess,
                "exec",
                {
                    "command": "sleep 0.5",
                    "workdir": "sub",
                    "description": "hold the sub/ scope lock",
                    "access": "write",
                },
            )
            order.append("exec")

        async def writer() -> None:
            # Give the exec a head start to acquire the scope lock first.
            await asyncio.sleep(0.15)
            await _call_tool(sess, "write", {"path": "sub/w.txt", "content": "W"})
            order.append("write")

        await asyncio.gather(exec_holder(), writer())

        assert order == ["exec", "write"]
        assert (root / "sub" / "w.txt").read_text() == "W"

    @pytest.mark.skipif(
        sys.platform == "win32", reason="POSIX shell (sleep) required"
    )
    async def test_read_access_exec_does_not_block_same_dir_write(
        self, tmp_path: Path
    ) -> None:
        """An ``access="read"`` exec takes NO lock, so a same-dir tool write
        stays fully parallel with it (completes before the read exec's sleep
        finishes) -- the read declaration is never worse than the baseline."""
        ws = await _materialise_local(tmp_path)
        sess = await ws.start_session(_binding())
        root = tmp_path_root(ws)
        (root / "sub").mkdir()

        order: list[str] = []

        async def exec_reader() -> None:
            await _call_tool(
                sess,
                "exec",
                {
                    "command": "sleep 0.5",
                    "workdir": "sub",
                    "description": "read-only, takes no lock",
                    "access": "read",
                },
            )
            order.append("exec")

        async def writer() -> None:
            await asyncio.sleep(0.15)
            await _call_tool(sess, "write", {"path": "sub/w.txt", "content": "W"})
            order.append("write")

        await asyncio.gather(exec_reader(), writer())

        assert order == ["write", "exec"]
