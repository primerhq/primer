"""Review 2026-11-23 finding 5.1 (local half): primer's own git runs with
hooks and the fsmonitor disabled, and without the API process environment.

The reserved ``.state/.git`` tree is agent-writable through ``exec`` (the
agent's file tools now refuse it, but a shell can plant there). Primer's
per-turn commit (``LocalStateRepo._run_git_once``) used to run ``git`` with
no overrides and the full process environment, so a planted hook ran as the
API process. Every git call now passes
``-c core.hooksPath=/dev/null -c core.fsmonitor=false
-c commit.gpgsign=false`` (a -c value
outranks any planted repo config) and a curated environment.

These tests plant the hooks/config DIRECTLY ON DISK -- not through the
agent tools -- so they prove the git-side half of the fix on its own.
"""

from __future__ import annotations

from pathlib import Path

from primer.workspace.local.state import LocalStateRepo


async def _repo(tmp_path: Path) -> LocalStateRepo:
    repo = LocalStateRepo(
        tmp_path / ".state", workspace_id="ws-1", subprocess_timeout_seconds=10.0
    )
    await repo.initialize()
    return repo


def _plant_hook(tmp_path: Path, name: str, marker: Path) -> Path:
    hook = tmp_path / ".state" / ".git" / "hooks" / name
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    return hook


async def test_a_planted_post_commit_hook_does_not_run(tmp_path: Path) -> None:
    repo = await _repo(tmp_path)
    marker = tmp_path / "hook-ran"
    _plant_hook(tmp_path, "post-commit", marker)

    sha = await repo.commit_arbitrary(summary="seed", files={"a.txt": "1"})
    assert sha  # primer's own commit still succeeds
    assert (tmp_path / ".state" / "a.txt").read_text() == "1"
    assert not marker.exists(), (
        "primer's own commit executed a planted git hook"
    )


async def test_a_planted_pre_commit_hook_does_not_run(tmp_path: Path) -> None:
    """A pre-commit hook could also break or spy on every commit."""
    repo = await _repo(tmp_path)
    marker = tmp_path / "pre-commit-ran"
    _plant_hook(tmp_path, "pre-commit", marker)

    await repo.commit_arbitrary(summary="seed", files={"a.txt": "1"})
    assert not marker.exists(), "primer's own commit executed a planted hook"


async def test_a_planted_fsmonitor_script_does_not_run(tmp_path: Path) -> None:
    """``core.fsmonitor`` named in the repo config would otherwise be
    executed by git's status/diff during the commit; the command-line
    ``-c core.fsmonitor=false`` outranks it."""
    repo = await _repo(tmp_path)
    marker = tmp_path / "fsmonitor-ran"
    script = tmp_path / "fsmonitor.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\n")
    script.chmod(0o755)
    config = tmp_path / ".state" / ".git" / "config"
    config.write_text(
        config.read_text() + f"\n[core]\n\tfsmonitor = {script}\n"
    )

    await repo.commit_arbitrary(summary="seed", files={"a.txt": "1"})
    assert not marker.exists(), (
        "primer's own commit executed a planted fsmonitor script"
    )


async def test_a_planted_gpg_program_does_not_run(tmp_path: Path) -> None:
    """``commit.gpgsign=true`` plus a planted ``gpg.program`` would run
    the planted program at every commit; -c commit.gpgsign=false
    outranks the planted config."""
    repo = await _repo(tmp_path)
    marker = tmp_path / "gpg-ran"
    script = tmp_path / "gpg.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\n")
    script.chmod(0o755)
    config = tmp_path / ".state" / ".git" / "config"
    config.write_text(
        config.read_text()
        + f"\n[commit]\n\tgpgsign = true\n\n[gpg]\n\tprogram = {script}\n"
    )

    sha = await repo.commit_arbitrary(summary="seed", files={"a.txt": "1"})
    assert sha  # the commit still succeeds, unsigned
    assert not marker.exists(), (
        "primer's own commit ran a planted gpg program"
    )


async def test_every_primer_git_call_gets_the_overrides_and_a_curated_env(
    tmp_path: Path, monkeypatch
) -> None:
    """Captured at the create_subprocess_exec seam: every git call primer
    spawns carries the hook/fsmonitor overrides and a curated environment
    that does not hold a process-level secret."""
    sentinel = "PRIMER_SENTINEL_DO_NOT_LEAK"
    monkeypatch.setenv(sentinel, "do-not-leak")

    import primer.workspace.local.state as state_mod

    calls: list[dict] = []
    real = state_mod.asyncio.create_subprocess_exec

    async def capture(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return await real(*args, **kwargs)

    repo = await _repo(tmp_path)

    # The seam is the asyncio MODULE attribute (state.py calls
    # asyncio.create_subprocess_exec, it holds no reference of its own), so the patch is
    # process-wide by construction. Scope it to the git call itself: patch, run ONE git
    # commit, restore at once, so no other subprocess spawn in this process is intercepted
    # outside that window (the monkeypatch fixture would otherwise hold the patch for the
    # whole test).
    state_mod.asyncio.create_subprocess_exec = capture
    try:
        await repo.commit_arbitrary(summary="seed", files={"a.txt": "1"})
    finally:
        state_mod.asyncio.create_subprocess_exec = real

    assert calls, "git was never spawned"
    for call in calls:
        argv = [str(a) for a in call["args"]]
        assert "core.hooksPath=/dev/null" in argv
        assert "core.fsmonitor=false" in argv
        assert "commit.gpgsign=false" in argv
        env = call["kwargs"]["env"]
        assert sentinel not in env, (
            "a git subprocess got the API process environment"
        )
        assert env.get("PATH"), "git lost PATH: the curated env is not usable"
