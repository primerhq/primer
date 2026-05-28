"""Tests for the seven concrete WorkspaceTool implementations.

One file because they share fixtures (workspace root + ToolCallContext +
real AgentSession). Per-tool sections separate the assertions.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

import pytest

from matrix.model.except_ import BadRequestError, ConflictError, NotFoundError
from matrix.model.workspace_session import AgentBinding

# Import the workspace package up-front so ToolCallContext.model_rebuild()
# runs before any test constructs one (see matrix/workspace/__init__.py).
import matrix.workspace as _workspace_pkg  # noqa: F401

from matrix.workspace.local.cache import LocalTruncationStore as TruncationStore
from matrix.workspace.local.state import LocalStateRepo as StateRepo
from matrix.workspace.session import AgentSession
from matrix.workspace.tool import ToolCallContext
from matrix.workspace.local.tools import (
    Edit,
    EditArgs,
    Exec,
    ExecArgs,
    Glob,
    GlobArgs,
    Grep,
    GrepArgs,
    Ls,
    LsArgs,
    Read,
    ReadArgs,
    Write,
    WriteArgs,
)


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git CLI not available on PATH (AgentSession needs it)",
)


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    return root


@pytest.fixture
async def session(tmp_path: Path) -> AgentSession:
    repo = StateRepo(tmp_path / ".state", workspace_id="ws-1")
    await repo.initialize()
    cache = TruncationStore(tmp_path / ".tmp")
    return await AgentSession.start(
        session_id="sess-1",
        workspace_id="ws-1",
        agent_binding=AgentBinding(agent_id="agent-foo", agent_name="Agent"),
        state_repo=repo,
        truncation_store=cache,
    )


@pytest.fixture
def ctx(session: AgentSession) -> ToolCallContext:
    return ToolCallContext(
        workspace_id="ws-1",
        session_id="sess-1",
        agent_id="agent-foo",
        call_id="c-1",
        abort=asyncio.Event(),
        session=session,
    )


# ===========================================================================
# ls
# ===========================================================================


class TestLs:
    async def test_lists_files(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        (workspace_root / "a.txt").write_text("aa")
        (workspace_root / "b.txt").write_text("bbb")
        (workspace_root / "sub").mkdir()
        result = await Ls(workspace_root).execute(LsArgs(), ctx)
        assert "f          2 a.txt" in result.output
        assert "f          3 b.txt" in result.output
        assert "d          0 sub" in result.output

    async def test_skips_dotfiles_by_default(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        (workspace_root / ".hidden").write_text("x")
        (workspace_root / "visible").write_text("x")
        result = await Ls(workspace_root).execute(LsArgs(), ctx)
        assert ".hidden" not in result.output
        assert "visible" in result.output

    async def test_show_hidden_includes_dotfiles(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        (workspace_root / ".env").write_text("x")
        result = await Ls(workspace_root).execute(
            LsArgs(show_hidden=True), ctx
        )
        assert ".env" in result.output

    async def test_recursive_lists_subdirs(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        (workspace_root / "src").mkdir()
        (workspace_root / "src" / "main.py").write_text("pass")
        result = await Ls(workspace_root).execute(LsArgs(recursive=True), ctx)
        assert "src/main.py" in result.output

    async def test_max_depth_caps_recursion(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        deep = workspace_root / "a" / "b" / "c"
        deep.mkdir(parents=True)
        (deep / "leaf.txt").write_text("x")
        result = await Ls(workspace_root).execute(
            LsArgs(recursive=True, max_depth=1), ctx
        )
        # depth=1 includes 'a' and stops; 'b' and 'leaf.txt' should NOT appear.
        assert "a/b" not in result.output
        assert "leaf.txt" not in result.output

    async def test_rejects_missing_path(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        with pytest.raises(NotFoundError):
            await Ls(workspace_root).execute(LsArgs(path="nope"), ctx)

    async def test_rejects_path_that_is_file(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        f = workspace_root / "a.txt"
        f.write_text("x")
        with pytest.raises(BadRequestError, match="not a directory"):
            await Ls(workspace_root).execute(LsArgs(path="a.txt"), ctx)

    async def test_rejects_path_escaping_root(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        with pytest.raises(BadRequestError, match="outside workspace"):
            await Ls(workspace_root).execute(LsArgs(path="../.."), ctx)


# ===========================================================================
# read
# ===========================================================================


class TestRead:
    async def test_reads_with_line_numbers(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        (workspace_root / "f.txt").write_text("alpha\nbeta\ngamma")
        result = await Read(workspace_root).execute(
            ReadArgs(path="f.txt"), ctx
        )
        assert result.output == "     1→alpha\n     2→beta\n     3→gamma"
        assert result.truncated is False

    async def test_offset_and_limit(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        (workspace_root / "f.txt").write_text("\n".join(f"line{i}" for i in range(10)))
        result = await Read(workspace_root).execute(
            ReadArgs(path="f.txt", offset=3, limit=2), ctx
        )
        assert "     4→line3" in result.output
        assert "     5→line4" in result.output
        assert "line5" not in result.output
        assert result.truncated is True

    async def test_binary_file_returns_summary(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        (workspace_root / "blob.bin").write_bytes(b"\x00\x01\x02\x03")
        result = await Read(workspace_root).execute(
            ReadArgs(path="blob.bin"), ctx
        )
        assert "<binary file:" in result.output
        assert result.truncated is True

    async def test_marks_read_on_session(
        self,
        workspace_root: Path,
        ctx: ToolCallContext,
        session: AgentSession,
    ) -> None:
        (workspace_root / "x.txt").write_text("hi")
        await Read(workspace_root).execute(ReadArgs(path="x.txt"), ctx)
        assert session.was_read("x.txt")

    async def test_rejects_missing_file(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        with pytest.raises(NotFoundError):
            await Read(workspace_root).execute(ReadArgs(path="nope"), ctx)

    async def test_rejects_directory(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        (workspace_root / "d").mkdir()
        with pytest.raises(BadRequestError, match="not a file"):
            await Read(workspace_root).execute(ReadArgs(path="d"), ctx)


# ===========================================================================
# write
# ===========================================================================


class TestWrite:
    async def test_creates_new_file(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        result = await Write(workspace_root).execute(
            WriteArgs(path="new.txt", content="hello"), ctx
        )
        assert "wrote 5 bytes" in result.output
        assert (workspace_root / "new.txt").read_text() == "hello"

    async def test_creates_parent_directories(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        await Write(workspace_root).execute(
            WriteArgs(path="a/b/c.txt", content="x"), ctx
        )
        assert (workspace_root / "a" / "b" / "c.txt").read_text() == "x"

    async def test_refuses_overwrite_without_read(
        self,
        workspace_root: Path,
        ctx: ToolCallContext,
    ) -> None:
        (workspace_root / "f.txt").write_text("original")
        with pytest.raises(ConflictError, match="read it first"):
            await Write(workspace_root).execute(
                WriteArgs(path="f.txt", content="overwritten"), ctx
            )
        assert (workspace_root / "f.txt").read_text() == "original"

    async def test_overwrites_after_read(
        self,
        workspace_root: Path,
        ctx: ToolCallContext,
    ) -> None:
        (workspace_root / "f.txt").write_text("original")
        await Read(workspace_root).execute(ReadArgs(path="f.txt"), ctx)
        await Write(workspace_root).execute(
            WriteArgs(path="f.txt", content="overwritten"), ctx
        )
        assert (workspace_root / "f.txt").read_text() == "overwritten"

    async def test_force_overrides_read_check(
        self,
        workspace_root: Path,
        ctx: ToolCallContext,
    ) -> None:
        (workspace_root / "f.txt").write_text("original")
        await Write(workspace_root).execute(
            WriteArgs(path="f.txt", content="forced", force=True), ctx
        )
        assert (workspace_root / "f.txt").read_text() == "forced"

    async def test_rejects_invalid_octal_mode(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        with pytest.raises(BadRequestError, match="octal"):
            await Write(workspace_root).execute(
                WriteArgs(path="x.txt", content="x", mode="not-a-number"), ctx
            )


# ===========================================================================
# edit
# ===========================================================================


class TestEdit:
    async def test_replaces_unique_old_string(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        (workspace_root / "f.py").write_text("def foo():\n    return 1\n")
        result = await Edit(workspace_root).execute(
            EditArgs(path="f.py", old_string="return 1", new_string="return 2"),
            ctx,
        )
        assert "-    return 1" in result.output
        assert "+    return 2" in result.output
        assert (workspace_root / "f.py").read_text() == "def foo():\n    return 2\n"

    async def test_rejects_non_unique_without_replace_all(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        (workspace_root / "f.py").write_text("x\nx\n")
        with pytest.raises(BadRequestError, match="non-unique"):
            await Edit(workspace_root).execute(
                EditArgs(path="f.py", old_string="x", new_string="y"), ctx
            )

    async def test_replace_all(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        (workspace_root / "f.py").write_text("x\nx\nx\n")
        await Edit(workspace_root).execute(
            EditArgs(
                path="f.py", old_string="x", new_string="y", replace_all=True
            ),
            ctx,
        )
        assert (workspace_root / "f.py").read_text() == "y\ny\ny\n"

    async def test_rejects_old_string_not_found(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        (workspace_root / "f.py").write_text("hello")
        with pytest.raises(BadRequestError, match="not found"):
            await Edit(workspace_root).execute(
                EditArgs(path="f.py", old_string="xyz", new_string="abc"), ctx
            )

    async def test_rejects_identical_strings(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        (workspace_root / "f.py").write_text("x")
        with pytest.raises(BadRequestError, match="identical"):
            await Edit(workspace_root).execute(
                EditArgs(path="f.py", old_string="x", new_string="x"), ctx
            )

    async def test_rejects_missing_file(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        with pytest.raises(NotFoundError):
            await Edit(workspace_root).execute(
                EditArgs(path="nope", old_string="a", new_string="b"), ctx
            )


# ===========================================================================
# glob
# ===========================================================================


class TestGlob:
    async def test_finds_matching_paths(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        (workspace_root / "src").mkdir()
        (workspace_root / "src" / "a.py").write_text("a")
        (workspace_root / "src" / "b.py").write_text("b")
        (workspace_root / "src" / "c.txt").write_text("c")
        result = await Glob(workspace_root).execute(
            GlobArgs(pattern="src/*.py"), ctx
        )
        lines = sorted(result.output.splitlines())
        assert lines == ["src/a.py", "src/b.py"]

    async def test_recursive_pattern(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        (workspace_root / "a" / "b").mkdir(parents=True)
        (workspace_root / "a" / "b" / "deep.py").write_text("x")
        result = await Glob(workspace_root).execute(
            GlobArgs(pattern="**/*.py"), ctx
        )
        assert "a/b/deep.py" in result.output

    async def test_sorts_newest_first(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        import os

        old = workspace_root / "old.py"
        new = workspace_root / "new.py"
        old.write_text("o")
        new.write_text("n")
        # Force mtimes so the test isn't flaky on coarse-resolution FS.
        os.utime(old, (1_000_000, 1_000_000))
        os.utime(new, (2_000_000, 2_000_000))
        result = await Glob(workspace_root).execute(GlobArgs(pattern="*.py"), ctx)
        assert result.output.splitlines() == ["new.py", "old.py"]

    async def test_pagination_limit_and_offset(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        for i in range(5):
            (workspace_root / f"f{i}.txt").write_text("x")
        result = await Glob(workspace_root).execute(
            GlobArgs(pattern="*.txt", limit=2, offset=1), ctx
        )
        assert len(result.output.splitlines()) == 2
        assert result.truncated is True

    async def test_returns_empty_for_no_matches(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        result = await Glob(workspace_root).execute(
            GlobArgs(pattern="*.nonexistent"), ctx
        )
        assert result.output == ""


# ===========================================================================
# grep
# ===========================================================================


class TestGrep:
    async def test_files_with_matches_default(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        (workspace_root / "a.py").write_text("def foo(): pass\n")
        (workspace_root / "b.py").write_text("def bar(): pass\n")
        (workspace_root / "c.py").write_text("class C: pass\n")
        result = await Grep(workspace_root).execute(
            GrepArgs(pattern=r"^def "), ctx
        )
        lines = sorted(result.output.splitlines())
        assert lines == ["a.py", "b.py"]

    async def test_content_mode_emits_path_lineno_text(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        (workspace_root / "a.py").write_text("hello\nfoo\nbar\n")
        result = await Grep(workspace_root).execute(
            GrepArgs(pattern="foo", output_mode="content"), ctx
        )
        assert "a.py:2:foo" in result.output

    async def test_count_mode(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        (workspace_root / "a.py").write_text("x\nx\nx\n")
        result = await Grep(workspace_root).execute(
            GrepArgs(pattern="x", output_mode="count"), ctx
        )
        assert result.output == "a.py:3"

    async def test_glob_filter(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        (workspace_root / "a.py").write_text("hit\n")
        (workspace_root / "a.txt").write_text("hit\n")
        result = await Grep(workspace_root).execute(
            GrepArgs(pattern="hit", glob="*.py"), ctx
        )
        assert result.output == "a.py"

    async def test_case_insensitive(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        (workspace_root / "f.txt").write_text("Hello World\n")
        result = await Grep(workspace_root).execute(
            GrepArgs(pattern="hello", case_insensitive=True), ctx
        )
        assert "f.txt" in result.output

    async def test_skips_binary_files(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        (workspace_root / "a.txt").write_text("findme\n")
        (workspace_root / "b.bin").write_bytes(b"findme\x00\x01\x02")
        result = await Grep(workspace_root).execute(
            GrepArgs(pattern="findme"), ctx
        )
        assert "a.txt" in result.output
        assert "b.bin" not in result.output

    async def test_content_mode_with_context(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        (workspace_root / "f.txt").write_text("a\nb\nMATCH\nc\nd\n")
        result = await Grep(workspace_root).execute(
            GrepArgs(pattern="MATCH", output_mode="content", context=1), ctx
        )
        assert "f.txt:2:b" in result.output
        assert "f.txt:3:MATCH" in result.output
        assert "f.txt:4:c" in result.output

    async def test_head_limit_truncates(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        for i in range(10):
            (workspace_root / f"f{i}.txt").write_text("hit\n")
        result = await Grep(workspace_root).execute(
            GrepArgs(pattern="hit", head_limit=3), ctx
        )
        assert len(result.output.splitlines()) == 3
        assert result.truncated is True

    async def test_invalid_regex_rejected(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        with pytest.raises(BadRequestError, match="invalid regex"):
            await Grep(workspace_root).execute(
                GrepArgs(pattern="[unclosed"), ctx
            )

    async def test_missing_path(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        with pytest.raises(NotFoundError):
            await Grep(workspace_root).execute(
                GrepArgs(pattern="x", path="nope"), ctx
            )


# ===========================================================================
# exec
# ===========================================================================


class TestExec:
    async def test_runs_command_and_returns_output(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        result = await Exec(workspace_root).execute(
            ExecArgs(
                command=f'"{sys.executable}" -c "print(\'hello\')"',
                description="say hi",
            ),
            ctx,
        )
        # body is "<rc>\n<stdout>\n<stderr>"
        rc_line, _, rest = result.output.partition("\n")
        assert rc_line == "0"
        assert "hello" in rest
        assert result.metadata["exit_code"] == 0

    async def test_non_zero_exit_returns_in_body(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        result = await Exec(workspace_root).execute(
            ExecArgs(
                command=f'"{sys.executable}" -c "import sys; sys.exit(7)"',
                description="exit 7",
            ),
            ctx,
        )
        assert result.output.startswith("7\n")
        assert result.metadata["exit_code"] == 7

    async def test_workdir_resolution(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        sub = workspace_root / "sub"
        sub.mkdir()
        (sub / "marker.txt").write_text("here")
        # Run a command that prints whether marker.txt exists in cwd.
        result = await Exec(workspace_root).execute(
            ExecArgs(
                command=(
                    f'"{sys.executable}" -c '
                    '"import os; print(os.path.exists(\'marker.txt\'))"'
                ),
                workdir="sub",
                description="check cwd",
            ),
            ctx,
        )
        assert "True" in result.output

    async def test_timeout_kills_process(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        with pytest.raises(BadRequestError, match="timed out"):
            await Exec(workspace_root).execute(
                ExecArgs(
                    command=(
                        f'"{sys.executable}" -c '
                        '"import time; time.sleep(10)"'
                    ),
                    timeout_ms=200,
                    description="long sleep",
                ),
                ctx,
            )

    async def test_background_not_supported(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        with pytest.raises(BadRequestError, match="background"):
            await Exec(workspace_root).execute(
                ExecArgs(
                    command="echo bg",
                    background=True,
                    description="bg",
                ),
                ctx,
            )

    async def test_missing_workdir(
        self, workspace_root: Path, ctx: ToolCallContext
    ) -> None:
        with pytest.raises(NotFoundError):
            await Exec(workspace_root).execute(
                ExecArgs(
                    command="echo x",
                    workdir="nope",
                    description="x",
                ),
                ctx,
            )
