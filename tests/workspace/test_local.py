"""Tests for primer.workspace.local.LocalWorkspaceBackend + LocalWorkspace."""

from __future__ import annotations

import asyncio
import contextlib
import io
import shlex
import shutil
import sys
import tarfile
import uuid
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


# The lock-ordering tests below prove "A does not block B" by holding A open on a handshake and showing B completes meanwhile, NOT by
# racing two wall-clock sleeps (the old shape: ``sleep 0.5`` exec vs a write after ``asyncio.sleep(0.15)`` and ``order ==
# ["write", "exec"]``, which failed in CI when a loaded runner's fsync took more than the ~340 ms margin: ticket 01a11545). Every wait
# in the handshake is bounded, so a broken lock FAILS within seconds and never hangs the lane.
_STARTED_BOUND_S = 10.0       # the held exec must start (a shell spawn on a loaded runner can take a while)
_WRITE_BOUND_S = 3.0          # a write that is NOT blocked takes milliseconds (measured: p95 under 60 ms, max about 230 ms under load)
_EXEC_FINISH_BOUND_S = 10.0   # once released, the held exec must end
_BODY_BOUND_S = 12.0          # EVERYTHING inside an `async with _exec_held_open(...)` block: no wait in a test body may outlive this


@contextlib.asynccontextmanager
async def _exec_held_open(session, root: Path, *, workdir: str, access: str):
    """Run an exec that is PROVABLY in flight until the ``with`` block ends, and yield its task.

    The command touches a ``started`` marker and then loops until a ``release`` marker exists, so inside the block the exec is running
    (the marker proves it started, nothing but the release lets it end) and the test controls exactly when it ends: no timing assumption.
    The markers live in ``root``, outside every workdir the tests lock.

    On EVERY path out (the body passed, the body failed, the exec never started) ``release`` is created, so the shell loop always exits and
    cannot be left spinning, the exec is awaited with a bound, and the markers are removed. A body that failed is never masked by how the
    exec ended; a body that passed fails the test if the exec errors or does not finish once released.

    The BODY is bounded too (``_BODY_BOUND_S``), so no wait inside the block, including one a test makes itself (taking a lock the exec
    holds, say), can hang the lane: it ends as a failure that says so. The block's own contexts unwind before the cleanup below runs, so a
    lock the test holds is released before the exec is awaited.
    """
    token = uuid.uuid4().hex[:8]
    started, release = root / f".held-started-{token}", root / f".held-release-{token}"
    command = f"touch {shlex.quote(str(started))}; until [ -e {shlex.quote(str(release))} ]; do sleep 0.01; done"
    task = asyncio.create_task(_call_tool(
        session, "exec",
        {"command": command, "workdir": workdir, "description": "held open by the test until released", "access": access},
    ))
    loop = asyncio.get_running_loop()

    def _cleanup() -> None:
        started.unlink(missing_ok=True)
        release.unlink(missing_ok=True)

    body_bound = asyncio.timeout(_BODY_BOUND_S)
    try:
        deadline = loop.time() + _STARTED_BOUND_S
        while not started.exists():
            if task.done():
                task.result()                      # the exec ended without starting: its own error says why
                pytest.fail("the held exec ended before it started", pytrace=False)
            if loop.time() > deadline:
                pytest.fail(f"the held exec did not start within {_STARTED_BOUND_S:g}s", pytrace=False)
            await asyncio.sleep(0.01)
        async with body_bound:
            yield task
    except BaseException:
        release.touch()                            # the loop must exit even though the test is failing
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, _EXEC_FINISH_BOUND_S)   # on a timeout wait_for cancels it, which kills its process group
        _cleanup()
        if body_bound.expired():
            pytest.fail(
                f"something inside the held-exec block did not finish within {_BODY_BOUND_S:g}s (a wait with no bound of its own)",
                pytrace=False,
            )
        raise
    release.touch()
    try:
        await asyncio.wait_for(task, _EXEC_FINISH_BOUND_S)
    except TimeoutError:
        pytest.fail(f"the exec did not finish within {_EXEC_FINISH_BOUND_S:g}s of being released", pytrace=False)
    finally:
        _cleanup()


async def _write_completes_while_held(session, exec_task: asyncio.Task, args: dict) -> None:
    """Run one write tool call and require it to complete while ``exec_task`` is still running.

    A write that is blocked behind the held exec can only finish after the release, which the test gives only after this returns, so a
    blocked write waits out ``_WRITE_BOUND_S`` and the test FAILS with a message that names the cause.
    """
    try:
        await asyncio.wait_for(_call_tool(session, "write", args), _WRITE_BOUND_S)
    except TimeoutError:
        pytest.fail(
            f"the write did not complete within {_WRITE_BOUND_S:g}s while the exec was still running: the exec is holding a lock "
            "the write needs", pytrace=False,
        )
    assert not exec_task.done(), "the exec ended before the write did, so the write was not shown to run alongside it"


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

    async def test_get_session_reads_a_tool_turn_cap_end_back_from_disk(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        """The dispatch mirrors a capped autonomous session's ENDED onto the on-disk slot with
        ended_reason "tool_turn_cap"; the slot must write it and read it back (SessionInfo validates the reason
        when it is loaded), because the MCP workspace tools read this slot."""
        ws = await provider.create(_template())
        session = await ws.start_session(_binding(), id="sess-cap-1")
        await session.set_status(SessionStatus.ENDED, ended_reason="tool_turn_cap")
        from primer.model.workspace_session import SessionStatus as _S
        session._info = session._info.model_copy(update={"status": _S.RUNNING})

        healed = await ws.get_session("sess-cap-1")

        assert await healed.status() == SessionStatus.ENDED
        assert (await healed.info()).ended_reason == "tool_turn_cap"

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

    async def test_list_files_recursive_max_entries_bounds_the_walk(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        """01a0644b: recursive=True used to materialise the whole subtree
        before any caller got to page/slice it - expensive on a large
        tree regardless of how few entries were actually wanted.
        max_entries caps the walk itself, not just a post-hoc slice."""
        tpl = _template(
            files=[
                FileMount(
                    path=f"d/f{i}.txt", source={"kind": "inline", "content": "x"},
                )
                for i in range(10)
            ]
        )
        ws = await provider.create(tpl)
        entries = await ws.list_files(".", recursive=True, max_entries=3)
        assert len(entries) == 3

    async def test_list_files_recursive_max_entries_none_is_unbounded(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        """max_entries=None (the default) preserves the pre-existing
        unbounded behaviour exactly - every direct caller of list_files
        that doesn't pass it (there are several outside the HTTP route)
        must see no change."""
        tpl = _template(
            files=[
                FileMount(
                    path=f"d/f{i}.txt", source={"kind": "inline", "content": "x"},
                )
                for i in range(10)
            ]
        )
        ws = await provider.create(tpl)
        entries = await ws.list_files(".", recursive=True)
        rels = {e.path for e in entries}
        assert len(rels) >= 10  # 10 files + the "d" directory entry itself

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
    """Closing a workspace HANDLE releases it; it ends no session (architecture review A-24).

    ``aclose`` used to commit ``session.json`` as ENDED / ``completed`` for every cached live session, and it runs whenever a handle
    is closed: at API and worker shutdown and on every workspace-provider invalidate. The durable row was untouched, so a parked or
    running session came back with a dead slot, and a wake or a reclaimed turn then raised ``ConflictError``. A session ends when
    its lifecycle says so (dispatch, cancel, delete); destroying the workspace ends the ones still on it, because the workspace
    is going away.
    """

    async def test_aclose_ends_no_session(self, provider: LocalWorkspaceBackend) -> None:
        ws = await provider.create(_template())
        s = await ws.start_session(_binding())

        await ws.aclose()

        assert await s.status() == SessionStatus.RUNNING
        again = await (await provider.get(ws.id)).get_session(s.session_id)
        info = await again.info()
        assert (info.status, info.ended_reason) == (SessionStatus.RUNNING, None), "the slot on disk was ended by a handle close"
        await again.append_instruction("a wake after the workspace handle was closed")

    async def test_aclose_leaves_a_waiting_session_waiting(self, provider: LocalWorkspaceBackend) -> None:
        from datetime import datetime, timezone

        from primer.model.workspace_session import _UserInputWaiting  # type: ignore[attr-defined]

        ws = await provider.create(_template())
        s = await ws.start_session(_binding())
        await s.set_status(
            SessionStatus.WAITING, waiting_state=_UserInputWaiting(prompt="?", queued_at=datetime.now(timezone.utc)),
        )

        await ws.aclose()

        again = await (await provider.get(ws.id)).get_session(s.session_id)
        assert (await again.info()).status == SessionStatus.WAITING

    async def test_backend_shutdown_leaves_live_sessions_live_for_the_next_process(self, tmp_path: Path) -> None:
        first = LocalWorkspaceBackend(tmp_path / "provider_root")
        await first.initialize()
        ws = await first.create(_template())
        s = await ws.start_session(_binding())
        await first.aclose()

        second = LocalWorkspaceBackend(tmp_path / "provider_root")
        await second.initialize()
        reattached = await second.get(ws.id, template=_template())
        again = await reattached.get_session(s.session_id)

        info = await again.info()
        assert (info.status, info.ended_reason) == (SessionStatus.RUNNING, None), "a restart ended the sessions on the disk"
        await again.append_instruction("a steer after the restart")

    async def test_destroy_still_ends_the_sessions_on_the_workspace(self, provider: LocalWorkspaceBackend) -> None:
        ws = await provider.create(_template())
        s = await ws.start_session(_binding())

        await provider.destroy(ws.id)

        assert await s.status() == SessionStatus.ENDED

    async def test_end_all_sessions_ends_what_is_live_and_tolerates_what_has_ended(
        self, provider: LocalWorkspaceBackend,
    ) -> None:
        ws = await provider.create(_template())
        live = await ws.start_session(_binding())
        done = await ws.start_session(_binding())
        await done.aclose()

        await ws.end_all_sessions()

        assert await live.status() == SessionStatus.ENDED and await done.status() == SessionStatus.ENDED

    async def test_end_all_sessions_goes_on_past_a_session_that_cannot_be_ended(
        self, provider: LocalWorkspaceBackend, caplog,
    ) -> None:
        """#488 review: the sandbox variant logs one session's failure and carries on; a destroy must not stop ending the rest
        because the first session's state repo is already broken."""
        import logging

        ws = await provider.create(_template())
        first = await ws.start_session(_binding())
        second = await ws.start_session(_binding())

        async def cannot_be_ended():
            raise OSError("the state repo is gone")

        first.aclose = cannot_be_ended  # type: ignore[method-assign]

        with caplog.at_level(logging.WARNING):
            await ws.end_all_sessions()

        assert await second.status() == SessionStatus.ENDED, "one failing session stopped the rest from being ended"
        assert any("ending a session failed" in r.getMessage() for r in caplog.records)

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


class TestMessagesJsonlAppendRewriteRace:
    """Event-row O_APPEND must not be clobbered by an instruction rewrite.

    ``AgentSession.append_instruction`` reads messages.jsonl, appends the
    user message, and rewrites the whole file. ``append_message_line``
    O_APPENDs a session event row. Without serialisation, an event row
    appended into the read->rewrite gap is truncated by the rewrite. Both
    paths acquire ``state_repo.messages_lock`` so the streamed row
    survives (see arch-review batch 1, Fix 2).
    """

    async def test_event_append_survives_instruction_rewrite(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws = await provider.create(_template())
        assert isinstance(ws, LocalWorkspace)
        session = await ws.start_session(
            _binding(), id="sess-race", instructions="hello"
        )
        sid = session.session_id
        rel = f"sessions/{sid}/messages.jsonl"
        event_line = b'{"seq":99,"kind":"assistant_token","text":"streamed-event"}\n'

        real_read = ws.state_repo.read_state_file
        append_started = asyncio.Event()
        append_tasks: list[asyncio.Task] = []
        fired = False

        async def _do_append() -> None:
            # Signal that the appender coroutine has begun, then perform the
            # O_APPEND. Under the fix this call blocks on messages_lock until
            # the rewrite releases it; without the fix it writes straight
            # into the read->rewrite gap.
            append_started.set()
            await ws.append_message_line(sid, event_line)

        async def hooked_read(path: str):
            nonlocal fired
            # Snapshot the file as the rewrite sees it FIRST (pre-append),
            # then open the read->rewrite gap so a concurrent event append
            # lands after this snapshot. The rewrite will write back this
            # snapshot -- if the appender is not serialised, its row is lost.
            result = await real_read(path)
            if path == rel and not fired:
                fired = True
                append_tasks.append(asyncio.create_task(_do_append()))
                await append_started.wait()
                # Give the (broken) unserialised append time to reach disk,
                # or the (fixed) serialised append time to block on the lock.
                await asyncio.sleep(0.05)
            return result

        # Shadow the shared state repo's read with the interleaving hook.
        ws.state_repo.read_state_file = hooked_read  # type: ignore[assignment]
        try:
            await session.append_instruction("steer me")
        finally:
            ws.state_repo.read_state_file = real_read  # type: ignore[assignment]
        await asyncio.gather(*append_tasks)

        content = (
            ws.root / ws.template.state_path / "sessions" / sid / "messages.jsonl"
        ).read_bytes()
        # The streamed event row appended into the read->rewrite window must
        # survive the full-file rewrite.
        assert b"streamed-event" in content, content
        # ...and the steering instruction rewrite must also be present.
        assert b"steer me" in content, content
        # ...along with the original instruction that seeded the file.
        assert b"hello" in content, content
        # The rewrite ran (interleave actually occurred).
        assert fired is True


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

    @pytest.mark.parametrize(
        "payload", ["echo hi ; echo INJECTED", "echo hi\necho INJECTED", "echo $(echo INJECTED)", "echo hi | cat", "echo hi > out.txt"],
    )
    async def test_shell_syntax_is_refused_not_executed(
        self, provider: LocalWorkspaceBackend, payload: str
    ) -> None:
        """A-06: the command used to run through ``/bin/sh -c`` with the route's first-token whitelist as the only guard."""
        ws = await provider.create(_template())
        assert isinstance(ws, LocalWorkspace)

        with pytest.raises(ValueError):
            await ws.diagnostic_exec(payload)

        assert not (ws.root / "out.txt").exists()

    async def test_the_command_does_not_see_the_primer_processs_environment(
        self, provider: LocalWorkspaceBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A-06: the diagnostic ran with ``{**os.environ, **workspace env}``, so a command in it could read whatever
        secrets the primer process holds. It now sees PATH and the workspace's own env only."""
        monkeypatch.setenv("PRIMER_DIAGNOSTIC_PROBE_SECRET", "topsecret")
        ws = await provider.create(_template())
        assert isinstance(ws, LocalWorkspace)

        result = await ws.diagnostic_exec("env")

        assert result.exit_code == 0
        assert "PRIMER_DIAGNOSTIC_PROBE_SECRET" not in result.stdout
        assert "PATH=" in result.stdout

    async def test_the_workspaces_own_env_reaches_the_program(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        """The other half of the minimal-env rule: the template env is still there (the env-injection e2e reads it back
        with ``printenv NAME``)."""
        ws = await provider.create(_template(env={"PRIMER_DIAGNOSTIC_TEMPLATE_VAR": "from-the-template"}))
        assert isinstance(ws, LocalWorkspace)

        result = await ws.diagnostic_exec("printenv PRIMER_DIAGNOSTIC_TEMPLATE_VAR")

        assert (result.exit_code, result.stdout) == (0, "from-the-template\n")

    async def test_a_program_that_does_not_exist_reports_127_like_a_shell_would(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws = await provider.create(_template())
        assert isinstance(ws, LocalWorkspace)

        result = await ws.diagnostic_exec("definitely-not-a-program-xyz")

        assert result.exit_code == 127 and "command not found" in result.stderr

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
        stays fully parallel with it -- the read declaration is never worse than
        the baseline.

        Proved with a handshake, not a race: the read exec is held open in
        ``sub/`` (it has started, and only the test can end it), and a write
        into the SAME directory must complete while it is still running. If
        the read exec ever took the same-dir scope lock, the write would wait
        behind it, and since the test releases the exec only after the write,
        it would never finish: the test fails after ``_WRITE_BOUND_S`` with a
        message naming the lock, not by an order that a slow disk could flip.
        """
        ws = await _materialise_local(tmp_path)
        sess = await ws.start_session(_binding())
        root = tmp_path_root(ws)
        (root / "sub").mkdir()

        async with _exec_held_open(sess, root, workdir="sub", access="read") as exec_task:
            await _write_completes_while_held(sess, exec_task, {"path": "sub/w.txt", "content": "W"})

        assert (root / "sub" / "w.txt").read_text() == "W"

    @pytest.mark.skipif(
        sys.platform == "win32", reason="POSIX shell (sleep) required"
    )
    async def test_strict_serializes_writes_in_different_dirs(
        self, tmp_path: Path
    ) -> None:
        """Under ``strict_write_locking=True`` every scope key collapses to the
        workspace ROOT, so two writes to DIFFERENT directories serialize.

        The ordering probe is what makes this behavioral rather than a wiring
        assertion: an exec holds ``a/`` for ~0.5s while a write to ``b/y.txt``
        (a different directory) is fired after a head start. Under strict both
        derive scope == <root>, so the write parks on the busy root scope lock
        and lands only AFTER the exec releases -> order == [exec, write]. The
        default-mode twin below proves the same pair overlaps when strict is
        off, so this test fails if strict ever silently no-ops.
        """
        ws = await _materialise_local(tmp_path, strict=True)
        sess = await ws.start_session(_binding())
        root = tmp_path_root(ws)
        (root / "a").mkdir()
        (root / "b").mkdir()

        order: list[str] = []

        async def exec_holder() -> None:
            await _call_tool(
                sess,
                "exec",
                {
                    "command": "sleep 0.5",
                    "workdir": "a",
                    "description": "hold the whole-root scope lock",
                    "access": "write",
                },
            )
            order.append("exec")

        async def writer() -> None:
            # Head start so the exec owns the root scope lock first.
            await asyncio.sleep(0.15)
            await _call_tool(sess, "write", {"path": "b/y.txt", "content": "Y"})
            order.append("write")

        await asyncio.gather(exec_holder(), writer())

        assert order == ["exec", "write"]
        assert (root / "b" / "y.txt").read_text() == "Y"

    @pytest.mark.skipif(
        sys.platform == "win32", reason="POSIX shell (sleep) required"
    )
    async def test_default_mode_writes_in_different_dirs_stay_concurrent(
        self, tmp_path: Path
    ) -> None:
        """The negative twin of the strict test: with the DEFAULT
        (``strict_write_locking=False``) workdir-scoped mode, an exec holding
        ``a/`` and a write to ``b/y.txt`` hold DIFFERENT scope keys, so they
        overlap: the write completes while the exec is still running.

        Together with the strict test this pins the exact behavior strict buys:
        same inputs, opposite outcome, decided solely by the template flag.
        Proved with the same handshake as the read-access test (the WRITING
        exec is held open in ``a/``, holding its scope lock; the write into
        ``b/`` must complete meanwhile), so no wall-clock order is involved.
        """
        ws = await _materialise_local(tmp_path, strict=False)
        sess = await ws.start_session(_binding())
        root = tmp_path_root(ws)
        (root / "a").mkdir()
        (root / "b").mkdir()

        async with _exec_held_open(sess, root, workdir="a", access="write") as exec_task:
            await _write_completes_while_held(sess, exec_task, {"path": "b/y.txt", "content": "Y"})

        assert (root / "b" / "y.txt").read_text() == "Y"

    # ---- the handshake's own guarantees (it must never be the thing that hangs or leaks) -------------------------------------

    @pytest.mark.skipif(
        sys.platform == "win32", reason="POSIX shell (until/sleep) required"
    )
    async def test_a_write_blocked_behind_the_held_exec_fails_within_the_bound_and_leaves_nothing_behind(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """What the two tests above rely on: if the write cannot complete while the exec is running, the test FAILS (it does not wait
        for the release it is the only one to give), says why, and the exec is still released and cleaned up.

        The write is blocked by the PATH lock of its own target, which the test holds itself: a lock the write takes and an exec never
        does, whatever the exec's locking is. That is deliberate. This test is about the HELPER, so it must not depend on the code under
        test: an earlier version took the same-dir SCOPE lock here, and when a read exec was made to take that lock (the very bug the two
        tests above exist to catch) the exec held it, the test's own acquire waited for it for ever, and the lane hung."""
        ws = await _materialise_local(tmp_path)
        sess = await ws.start_session(_binding())
        root = tmp_path_root(ws)
        (root / "sub").mkdir()
        monkeypatch.setattr(sys.modules[__name__], "_WRITE_BOUND_S", 0.3)

        started = asyncio.get_running_loop().time()
        with pytest.raises(pytest.fail.Exception, match="holding a lock the write needs"):
            async with _exec_held_open(sess, root, workdir="sub", access="read") as exec_task:
                async with ws._locks.hold_path(str((root / "sub" / "w.txt").resolve())):   # the key the write takes: it resolves the root
                    await _write_completes_while_held(sess, exec_task, {"path": "sub/w.txt", "content": "W"})

        assert asyncio.get_running_loop().time() - started < 5.0, "the failure was not prompt"
        assert exec_task.done(), "the held exec was left running"
        assert not list(root.glob(".held-*")), "the handshake markers were left behind"
        assert not (root / "sub" / "w.txt").exists(), "the cancelled write still landed"

    @pytest.mark.skipif(
        sys.platform == "win32", reason="POSIX shell (until/sleep) required"
    )
    async def test_a_failing_body_still_releases_the_held_exec_and_removes_the_markers(self, tmp_path: Path) -> None:
        ws = await _materialise_local(tmp_path)
        sess = await ws.start_session(_binding())
        root = tmp_path_root(ws)
        (root / "sub").mkdir()

        with pytest.raises(RuntimeError, match="the body failed"):
            async with _exec_held_open(sess, root, workdir="sub", access="read") as exec_task:
                raise RuntimeError("the body failed")

        assert exec_task.done() and exec_task.exception() is None, "the exec must end cleanly, not be left spinning"
        assert not list(root.glob(".held-*"))

    @pytest.mark.skipif(
        sys.platform == "win32", reason="POSIX shell (until/sleep) required"
    )
    async def test_a_wait_with_no_bound_inside_the_block_fails_within_the_body_bound_and_cleans_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The rule the handshake keeps: nothing a test does inside the block may hang the lane. A body that waits for something that
        never happens (here an event nobody sets, standing in for a lock the held exec never gives up) ends as a failure naming the
        bound, and the exec is released and cleaned up."""
        ws = await _materialise_local(tmp_path)
        sess = await ws.start_session(_binding())
        root = tmp_path_root(ws)
        (root / "sub").mkdir()
        monkeypatch.setattr(sys.modules[__name__], "_BODY_BOUND_S", 0.3)

        started = asyncio.get_running_loop().time()
        with pytest.raises(pytest.fail.Exception, match="did not finish within 0.3s"):
            async with _exec_held_open(sess, root, workdir="sub", access="read") as exec_task:
                await asyncio.Event().wait()

        assert asyncio.get_running_loop().time() - started < 5.0, "the failure was not prompt"
        assert exec_task.done() and exec_task.exception() is None, "the held exec was left running or errored"
        assert not list(root.glob(".held-*"))

    @pytest.mark.skipif(
        sys.platform == "win32", reason="POSIX shell (until/sleep) required"
    )
    async def test_an_exec_that_cannot_start_fails_with_its_own_error_and_leaves_nothing(self, tmp_path: Path) -> None:
        ws = await _materialise_local(tmp_path)
        sess = await ws.start_session(_binding())
        root = tmp_path_root(ws)

        with pytest.raises(NotFoundError, match="workdir"):
            async with _exec_held_open(sess, root, workdir="no-such-dir", access="read"):
                pytest.fail("the body must not run when the exec never started")

        assert not list(root.glob(".held-*"))


class TestMessagesLockIsPerSession:
    """The messages lock must not couple unrelated sessions.

    messages.jsonl is per-SESSION, so only writers to the same
    ``sessions/<id>/messages.jsonl`` need to serialise. A per-repo
    (per-workspace) lock would make session B's event-row flush wait on
    session A's git commit -- and because the flush runs inline in the
    worker's turn task, that stalls B's turn on unrelated work. The lock
    is keyed by session id (see arch-review batch 1, MEDIUM-3).
    """

    async def test_distinct_sessions_do_not_block_each_other(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        ws = await provider.create(_template())
        assert isinstance(ws, LocalWorkspace)
        sess_a = await ws.start_session(
            _binding(), id="sess-key-a", instructions="hello"
        )
        sess_b = await ws.start_session(
            _binding(), id="sess-key-b", instructions="hello"
        )

        # Hold session A's messages lock, standing in for A's in-flight
        # read->rewrite window (an append_instruction / turn commit).
        async with ws.state_repo.messages_lock(sess_a.session_id):
            # A DIFFERENT session's event-row flush must not wait on it.
            await asyncio.wait_for(
                ws.append_message_line(
                    sess_b.session_id, b'{"seq":1,"kind":"done"}\n'
                ),
                timeout=2.0,
            )

            # ...while the SAME session's flush must still serialise: it
            # blocks until A's window closes, so a bounded wait times out.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    ws.append_message_line(
                        sess_a.session_id, b'{"seq":1,"kind":"done"}\n'
                    ),
                    timeout=0.2,
                )

        b_path = (
            ws.root
            / ws.template.state_path
            / "sessions"
            / sess_b.session_id
            / "messages.jsonl"
        )
        assert b'{"seq":1,"kind":"done"}' in b_path.read_bytes()

        # Once A's window closes, A's own append proceeds normally.
        await asyncio.wait_for(
            ws.append_message_line(
                sess_a.session_id, b'{"seq":2,"kind":"done"}\n'
            ),
            timeout=2.0,
        )
        a_path = (
            ws.root
            / ws.template.state_path
            / "sessions"
            / sess_a.session_id
            / "messages.jsonl"
        )
        assert b'{"seq":2,"kind":"done"}' in a_path.read_bytes()

    async def test_same_session_key_shared_across_repo_and_session(
        self, provider: LocalWorkspaceBackend
    ) -> None:
        """The session handle and the repo must resolve the SAME lock.

        ``AgentSession.messages_lock`` keys the table by its own session
        id; ``append_message_line`` keys it by the id it was handed. If the
        two ever diverged the serialisation would silently do nothing.
        """
        ws = await provider.create(_template())
        assert isinstance(ws, LocalWorkspace)
        session = await ws.start_session(
            _binding(), id="sess-key-same", instructions="hello"
        )

        async with session.messages_lock:
            # The repo-keyed acquisition for the same session must block.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    ws.append_message_line(
                        session.session_id, b'{"seq":1,"kind":"done"}\n'
                    ),
                    timeout=0.2,
                )
