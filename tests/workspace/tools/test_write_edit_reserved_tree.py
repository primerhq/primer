"""Review 2026-11-23 finding 5.1 (local half), fix round 1: the agent
``write`` and ``edit`` tools refuse ONLY the workspace's git-internals
directory (``.state/.git``) -- the directory primer's own per-turn commit
reads, so a file planted there (a git hook, a clobbered config) is
executed or parsed by the API process later.

The check (``primer/workspace/local/tools/_common.py::inside_git_dir``)
compares against the REAL directory with :func:`os.path.samefile`, not a
string prefix: on a case-insensitive filesystem (macOS APFS)
``.state/.GIT/...`` and ``.state/./.git/...`` reach the same directory,
and a symlink inside the workspace pointing into it is refused too. The
rest of the ``.state`` tree (``.state/shared/`` notes,
``.state/sessions/<sid>/`` state) and ``.tmp`` are agent-writable by
design.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import primer.workspace  # noqa: F401  (ToolCallContext model_rebuild)
from primer.model.except_ import BadRequestError
from primer.workspace.local.tools import Edit, EditArgs, Write, WriteArgs


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(
        session=SimpleNamespace(
            was_read=lambda p: True, mark_read=lambda p: None
        )
    )


def _fs_case_insensitive() -> bool:
    d = Path(tempfile.mkdtemp())
    (d / "case-probe").mkdir()
    return (d / "CASE-PROBE").is_dir()


CASE_INSENSITIVE = _fs_case_insensitive()


def _git_dir(root: Path) -> Path:
    (root / ".state" / ".git" / "hooks").mkdir(parents=True)
    return root / ".state" / ".git"


# --- refusals: the git-internals directory only ---------------------------


@pytest.mark.asyncio
async def test_write_refuses_a_git_hook_on_a_fresh_root(tmp_path: Path) -> None:
    """Nothing exists yet (no .state at all): the resolved containment
    fallback still refuses the plant."""
    root = tmp_path / "ws"
    hook = root / ".state" / ".git" / "hooks" / "post-commit"
    with pytest.raises(BadRequestError, match="git directory"):
        await Write(root).execute(
            WriteArgs(
                path=".state/.git/hooks/post-commit",
                content="#!/bin/sh\nenv > leak.txt\n",
                mode="0755",
            ),
            _ctx(),
        )
    assert not hook.exists()


@pytest.mark.asyncio
async def test_write_refuses_clobbering_the_git_config(tmp_path: Path) -> None:
    """A clobbered config is parsed by primer's own git on every call."""
    root = tmp_path / "ws"
    git_dir = _git_dir(root)
    (git_dir / "config").write_text("[core]\n\tnaked = true\n")
    with pytest.raises(BadRequestError, match="git directory"):
        await Write(root).execute(
            WriteArgs(path=".state/.git/config", content="planted"), _ctx()
        )
    assert (git_dir / "config").read_text() == "[core]\n\tnaked = true\n"


@pytest.mark.asyncio
async def test_edit_refuses_a_file_inside_the_git_dir(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    git_dir = _git_dir(root)
    (git_dir / "config").write_text("original")
    with pytest.raises(BadRequestError, match="git directory"):
        await Edit(root).execute(
            EditArgs(
                path=".state/.git/config",
                old_string="original",
                new_string="replaced",
            ),
            _ctx(),
        )
    assert (git_dir / "config").read_text() == "original"


@pytest.mark.skipif(
    not CASE_INSENSITIVE,
    reason="the case variant only reaches the same directory on a case-insensitive filesystem",
)
@pytest.mark.asyncio
async def test_write_refuses_a_case_variant_of_the_git_dir(tmp_path: Path) -> None:
    """``.state/.GIT/...`` is the SAME directory as ``.state/.git`` on
    macOS APFS: a string-prefix check would miss it, samefile does not."""
    root = tmp_path / "ws"
    _git_dir(root)
    with pytest.raises(BadRequestError, match="git directory"):
        await Write(root).execute(
            WriteArgs(
                path=".state/.GIT/hooks/post-commit",
                content="#!/bin/sh\ntouch ran\n",
                mode="0755",
            ),
            _ctx(),
        )
    assert not (root / ".state" / ".GIT" / "hooks" / "post-commit").exists()


@pytest.mark.asyncio
async def test_write_refuses_a_dot_segment_path_into_the_git_dir(
    tmp_path: Path
) -> None:
    """``.state/./.git/...`` normalises into the git directory."""
    root = tmp_path / "ws"
    _git_dir(root)
    with pytest.raises(BadRequestError, match="git directory"):
        await Write(root).execute(
            WriteArgs(
                path=".state/./.git/hooks/pre-commit",
                content="#!/bin/sh\ntouch ran\n",
                mode="0755",
            ),
            _ctx(),
        )
    assert not (root / ".state" / ".git" / "hooks" / "pre-commit").exists()


@pytest.mark.asyncio
async def test_write_refuses_a_symlink_pointing_into_the_git_dir(
    tmp_path: Path
) -> None:
    """A symlink inside the workspace that points into .state/.git is
    refused: the resolved target lands in the git directory."""
    root = tmp_path / "ws"
    _git_dir(root)
    (root / "git-hooks").symlink_to(root / ".state" / ".git" / "hooks")
    with pytest.raises(BadRequestError, match="git directory"):
        await Write(root).execute(
            WriteArgs(
                path="git-hooks/post-commit",
                content="#!/bin/sh\ntouch ran\n",
                mode="0755",
            ),
            _ctx(),
        )
    assert not (root / ".state" / ".git" / "hooks" / "post-commit").exists()


@pytest.mark.asyncio
async def test_edit_refuses_a_file_symlink_into_the_git_dir(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    _git_dir(root)
    (root / ".state" / ".git" / "config").write_text("original")
    (root / "configlink").symlink_to(root / ".state" / ".git" / "config")
    with pytest.raises(BadRequestError, match="git directory"):
        await Edit(root).execute(
            EditArgs(
                path="configlink", old_string="original", new_string="planted"
            ),
            _ctx(),
        )
    assert (root / ".state" / ".git" / "config").read_text() == "original"


# --- benign cases that must still work -------------------------------------


@pytest.mark.asyncio
async def test_write_allows_state_shared(tmp_path: Path) -> None:
    """.state/shared/ is the documented cross-session handoff."""
    root = tmp_path / "ws"
    result = await Write(root).execute(
        WriteArgs(path=".state/shared/handoff.json", content="{}"), _ctx()
    )
    assert "wrote" in result.output
    assert (root / ".state" / "shared" / "handoff.json").read_text() == "{}"


@pytest.mark.asyncio
async def test_write_allows_session_todos(tmp_path: Path) -> None:
    """.state/sessions/<sid>/todos.json is the session's todo list."""
    root = tmp_path / "ws"
    result = await Write(root).execute(
        WriteArgs(path=".state/sessions/sess-abc123/todos.json", content="[]"),
        _ctx(),
    )
    assert (root / ".state" / "sessions" / "sess-abc123" / "todos.json").read_text() == "[]"


@pytest.mark.asyncio
async def test_write_allows_a_name_that_merely_starts_with_dot_state(
    tmp_path: Path
) -> None:
    """``notes/.state-backup.md`` sits outside the git directory: the
    check is real-directory containment, not a name prefix."""
    root = tmp_path / "ws"
    result = await Write(root).execute(
        WriteArgs(path="notes/.state-backup.md", content="keep"), _ctx()
    )
    assert (root / "notes" / ".state-backup.md").read_text() == "keep"


@pytest.mark.asyncio
async def test_write_allows_a_root_dot_gitignore(tmp_path: Path) -> None:
    """A ``.gitignore`` at the workspace root is the user's file, not the
    state repo's git directory."""
    root = tmp_path / "ws"
    result = await Write(root).execute(
        WriteArgs(path=".gitignore", content="*.log\n"), _ctx()
    )
    assert (root / ".gitignore").read_text() == "*.log\n"


@pytest.mark.asyncio
async def test_write_allows_the_tmp_tree(tmp_path: Path) -> None:
    """Only the git directory is refused: .tmp is the agent's own cache."""
    root = tmp_path / "ws"
    result = await Write(root).execute(
        WriteArgs(path=".tmp/scratch.txt", content="x"), _ctx()
    )
    assert (root / ".tmp" / "scratch.txt").read_text() == "x"


@pytest.mark.asyncio
async def test_edit_allows_state_shared(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    (root / ".state" / "shared").mkdir(parents=True)
    (root / ".state" / "shared" / "notes.md").write_text("one\ntwo\n")
    result = await Edit(root).execute(
        EditArgs(
            path=".state/shared/notes.md", old_string="two", new_string="three"
        ),
        _ctx(),
    )
    assert (root / ".state" / "shared" / "notes.md").read_text() == "one\nthree\n"


@pytest.mark.asyncio
async def test_write_and_edit_of_ordinary_files_still_work(tmp_path: Path) -> None:
    result = await Write(tmp_path).execute(
        WriteArgs(path="a.txt", content="one\ntwo\n"), _ctx()
    )
    assert "wrote 8 bytes" in result.output
    result = await Edit(tmp_path).execute(
        EditArgs(path="a.txt", old_string="two", new_string="three"), _ctx()
    )
    assert (tmp_path / "a.txt").read_text() == "one\nthree\n"


@pytest.mark.asyncio
async def test_materialised_workspace_tools_refuse_the_template_git_dir(
    tmp_path: Path
) -> None:
    """The tools a materialised workspace builds refuse the TEMPLATE's own
    git directory (here ``.statey/.git``) and nothing else."""
    from primer.model.workspace import ResourceLimits, WorkspaceTemplate
    from primer.workspace import LocalWorkspace

    template = WorkspaceTemplate(
        id="dev",
        description="d",
        provider_id="local-1",
        resources=ResourceLimits(),
        state_path=".statey",
        tmp_path=".tmpy",
    )
    root = tmp_path / "ws"
    root.mkdir()
    ws = await LocalWorkspace.materialise(
        workspace_id="ws-1", root=root, template=template, env={}
    )
    try:
        assert (root / ".statey" / ".git").is_dir(), "materialise did not init the state repo"
        write = next(t for t in ws.get_tools() if t.id == "write")
        with pytest.raises(BadRequestError, match="git directory"):
            await write.execute(
                WriteArgs(
                    path=".statey/.git/hooks/post-commit",
                    content="#!/bin/sh\ntouch ran\n",
                    mode="0755",
                ),
                _ctx(),
            )
        # the git directory's siblings and the DEFAULT state path are fine
        result = await write.execute(
            WriteArgs(path=".statey/plain.txt", content="ok"), _ctx()
        )
        assert (root / ".statey" / "plain.txt").read_text() == "ok"
        result = await write.execute(
            WriteArgs(path=".state/plain.txt", content="ok"), _ctx()
        )
        assert (root / ".state" / "plain.txt").read_text() == "ok"
    finally:
        await ws.aclose()
