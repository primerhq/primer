"""W14: a git that times out is stopped so its lock goes with it, a stale lock is cleared, and session.json is replaced atomically."""

from __future__ import annotations

import os
import time

import pytest

from primer.model.except_ import SubprocessTimeoutError
from primer.workspace.local import state as state_mod
from primer.workspace.local.state import LocalStateRepo, _GitCommandError


async def _repo(tmp_path, timeout=1.0) -> LocalStateRepo:
    repo = LocalStateRepo(tmp_path / ".state", workspace_id="ws-1", subprocess_timeout_seconds=timeout)
    await repo.initialize()
    await repo.commit_arbitrary(summary="seed", files={"a.txt": "1"})
    return repo


async def test_a_timed_out_git_does_not_wedge_later_commits(tmp_path, monkeypatch):
    editor = tmp_path / "slow-editor.sh"
    editor.write_text("#!/bin/sh\nsleep 4\n")
    editor.chmod(0o755)
    monkeypatch.setenv("GIT_EDITOR", str(editor))
    repo = await _repo(tmp_path)
    (tmp_path / ".state" / "a.txt").write_text("changed")
    with pytest.raises(SubprocessTimeoutError):
        # an explicit identity: a CI runner has no global git user, and git checks it before it opens the editor
        await repo._run_git("-c", "user.name=t", "-c", "user.email=t@example.com", "-c", f"core.editor={editor}", "commit", "-a", "--allow-empty")
    assert not (tmp_path / ".state" / ".git" / "index.lock").exists()
    monkeypatch.undo()
    await repo.commit_arbitrary(summary="next", files={"b.txt": "2"})


async def test_a_stale_index_lock_is_cleared_and_the_commit_retried(tmp_path):
    repo = await _repo(tmp_path, timeout=1.0)
    lock = tmp_path / ".state" / ".git" / "index.lock"
    lock.write_text("")
    old = time.time() - 60
    os.utime(lock, (old, old))
    await repo.commit_arbitrary(summary="after the crash", files={"b.txt": "2"})
    assert not lock.exists()


async def test_a_young_index_lock_is_left_alone(tmp_path):
    repo = await _repo(tmp_path, timeout=60.0)
    lock = tmp_path / ".state" / ".git" / "index.lock"
    lock.write_text("")  # a live git may hold it
    with pytest.raises(_GitCommandError):
        await repo.commit_arbitrary(summary="blocked", files={"b.txt": "2"})
    assert lock.exists()


async def test_commit_replaces_files_atomically(tmp_path, monkeypatch):
    repo = await _repo(tmp_path)
    replaced: list[str] = []
    real = os.replace
    monkeypatch.setattr(state_mod.os, "replace", lambda a, b: (replaced.append(os.path.basename(b)), real(a, b))[1])
    repo._agent_by_session["s-1"] = "ag"
    await repo.commit("s-1", summary="x", op="message", files={"session.json": "{}"})
    assert replaced == ["session.json"]
    assert (tmp_path / ".state" / "sessions" / "s-1" / "session.json").read_text() == "{}"
    assert not list((tmp_path / ".state" / "sessions" / "s-1").glob(".*.tmp"))


async def test_a_materialised_workspace_sweeps_old_tool_outputs_and_stops_on_aclose(tmp_path, monkeypatch):
    """W41: the retention sweep runs for a workspace nobody ends sessions on, and is cancelled with the handle."""
    import asyncio
    import functools

    from primer.model.workspace import ResourceLimits, WorkspaceTemplate
    from primer.workspace import LocalWorkspace
    from primer.workspace.local.cache import LocalTruncationStore

    monkeypatch.setattr(
        LocalTruncationStore, "start_background_cleanup",
        functools.partialmethod(LocalTruncationStore.start_background_cleanup, interval_seconds=0.05),
    )
    template = WorkspaceTemplate(id="dev", description="d", provider_id="local-1", resources=ResourceLimits())
    root = tmp_path / "ws"
    root.mkdir()
    ws = await LocalWorkspace.materialise(workspace_id="ws-1", root=root, template=template, env={})
    session_dir = root / template.tmp_path / "main"
    session_dir.mkdir(parents=True)
    old = session_dir / f"tool_{time.time_ns() - 30 * 24 * 3600 * 10**9}_00000001.txt"
    young = session_dir / f"tool_{time.time_ns()}_00000002.txt"
    old.write_text("old")
    young.write_text("young")
    for _ in range(100):
        if not old.exists():
            break
        await asyncio.sleep(0.05)
    assert not old.exists() and young.exists()
    task = ws._cleanup_task
    await ws.aclose()
    assert task.cancelled() or task.done()
