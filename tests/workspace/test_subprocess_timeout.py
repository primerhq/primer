"""Unit tests for the subprocess timeout feature.

Covers:
- LocalStateRepo: git subprocess exceeds tiny timeout -> raises SubprocessTimeoutError
- LocalStateRepo: git subprocess completes within timeout -> succeeds
- LocalWorkspaceBackend: init_command exceeds tiny timeout -> raises SubprocessTimeoutError
- LocalWorkspaceBackend: init_command completes within timeout -> succeeds
- AppConfig: subprocess_timeout_seconds field has the expected default
- AppConfig: PRIMER_SUBPROCESS_TIMEOUT_SECONDS env var overrides the default
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from primer.model.except_ import SubprocessTimeoutError
from primer.workspace.local.state import LocalStateRepo


# ===========================================================================
# Helpers
# ===========================================================================


def _git_available() -> bool:
    return shutil.which("git") is not None


pytestmark = pytest.mark.skipif(
    not _git_available(),
    reason="git CLI not available on PATH",
)


# ===========================================================================
# AppConfig field tests (no git needed)
# ===========================================================================


def test_appconfig_subprocess_timeout_default():
    """AppConfig.subprocess_timeout_seconds defaults to 120.0."""
    from primer.api.config import AppConfig

    cfg = AppConfig()
    assert cfg.subprocess_timeout_seconds == 120.0


def test_appconfig_subprocess_timeout_env_override(monkeypatch):
    """PRIMER_SUBPROCESS_TIMEOUT_SECONDS env var overrides the default."""
    from primer.api.config import AppConfig

    monkeypatch.setenv("PRIMER_SUBPROCESS_TIMEOUT_SECONDS", "30.0")
    cfg = AppConfig()
    assert cfg.subprocess_timeout_seconds == 30.0


def test_appconfig_subprocess_timeout_yaml_override(tmp_path, monkeypatch):
    """subprocess_timeout_seconds: in config.yaml overrides the default."""
    from primer.api.config import AppConfig

    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("subprocess_timeout_seconds = 45.0\n")
    monkeypatch.setenv("PRIMER_CONFIG_PATH", str(cfg_file))
    # Unset any stray env override so TOML wins.
    monkeypatch.delenv("PRIMER_SUBPROCESS_TIMEOUT_SECONDS", raising=False)
    cfg = AppConfig()
    assert cfg.subprocess_timeout_seconds == 45.0


# ===========================================================================
# LocalStateRepo timeout tests
# ===========================================================================


@pytest.mark.asyncio
async def test_state_repo_git_timeout_raises(tmp_path: Path, monkeypatch):
    """A git subprocess that exceeds the timeout raises SubprocessTimeoutError.

    We monkeypatch asyncio.create_subprocess_exec to return a sleeping process
    so the timeout fires reliably regardless of how fast git init runs.
    """
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    async def _slow_communicate():
        # Never returns within the test's tiny timeout
        await asyncio.sleep(60)
        return b"", b""

    mock_proc = MagicMock()
    mock_proc.communicate = _slow_communicate
    mock_proc.kill = MagicMock()
    mock_proc.wait = AsyncMock(return_value=None)

    repo_path = tmp_path / ".state"
    repo = LocalStateRepo(
        repo_path,
        workspace_id="ws-test",
        subprocess_timeout_seconds=0.05,
    )

    with patch("primer.workspace.local.state.asyncio.create_subprocess_exec", return_value=mock_proc):
        with pytest.raises(SubprocessTimeoutError) as exc_info:
            await repo._run_git_bytes("init", "--initial-branch=main")

    assert "timed out" in str(exc_info.value).lower()
    mock_proc.kill.assert_called_once()


@pytest.mark.asyncio
async def test_state_repo_git_succeeds_within_timeout(tmp_path: Path):
    """A git subprocess that completes within the timeout returns normally."""
    repo_path = tmp_path / ".state"
    repo = LocalStateRepo(
        repo_path,
        workspace_id="ws-test",
        subprocess_timeout_seconds=30.0,
    )
    # Initialise the repo (fast, should complete well within 30 s).
    await repo.initialize()
    # Verify the repo is usable (history returns empty list, not an error).
    commits = await repo.history()
    assert commits == []


@pytest.mark.asyncio
async def test_state_repo_timeout_error_type(tmp_path: Path):
    """SubprocessTimeoutError is a subclass of PrimerError."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch
    from primer.model.except_ import PrimerError

    async def _slow_communicate():
        await asyncio.sleep(60)
        return b"", b""

    mock_proc = MagicMock()
    mock_proc.communicate = _slow_communicate
    mock_proc.kill = MagicMock()
    mock_proc.wait = AsyncMock(return_value=None)

    repo_path = tmp_path / ".state"
    repo = LocalStateRepo(
        repo_path,
        workspace_id="ws-test",
        subprocess_timeout_seconds=0.05,
    )
    with patch("primer.workspace.local.state.asyncio.create_subprocess_exec", return_value=mock_proc):
        with pytest.raises(PrimerError):
            await repo._run_git_bytes("init", "--initial-branch=main")


# ===========================================================================
# LocalWorkspaceBackend init_command timeout tests
# ===========================================================================


@pytest.mark.asyncio
async def test_backend_init_command_timeout_raises(tmp_path: Path):
    """An init_command that exceeds the timeout raises SubprocessTimeoutError."""
    from primer.workspace.local.backend import LocalWorkspaceBackend

    backend = LocalWorkspaceBackend(
        root=tmp_path,
        subprocess_timeout_seconds=0.001,
    )
    ws_root = tmp_path / "ws-test"
    ws_root.mkdir()

    with pytest.raises(SubprocessTimeoutError) as exc_info:
        await backend._run_init_command(
            ws_root,
            "sleep 30",
            {},
        )

    assert "timed out" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_backend_init_command_succeeds_within_timeout(tmp_path: Path):
    """An init_command that completes within the timeout returns normally."""
    from primer.workspace.local.backend import LocalWorkspaceBackend

    backend = LocalWorkspaceBackend(
        root=tmp_path,
        subprocess_timeout_seconds=30.0,
    )
    ws_root = tmp_path / "ws-test"
    ws_root.mkdir()

    # Fast command: should succeed well within 30 s.
    await backend._run_init_command(
        ws_root,
        "echo hello",
        {},
    )


@pytest.mark.asyncio
async def test_backend_init_command_timeout_kills_process(tmp_path: Path):
    """A timed-out init_command kills the process group and returns promptly.

    Uses a 0.2s timeout against ``sleep 30`` to verify the call completes
    well before the sleep would naturally finish.
    """
    import time
    from primer.workspace.local.backend import LocalWorkspaceBackend

    backend = LocalWorkspaceBackend(
        root=tmp_path,
        subprocess_timeout_seconds=0.2,
    )
    ws_root = tmp_path / "ws-test"
    ws_root.mkdir()

    start = time.monotonic()
    with pytest.raises(SubprocessTimeoutError):
        await backend._run_init_command(ws_root, "sleep 30", {})
    elapsed = time.monotonic() - start

    # Should have returned well before the 30-second sleep finished.
    # Allow up to 10 seconds for the kill + wait cycle to complete.
    assert elapsed < 10.0, f"Expected prompt return after kill, took {elapsed:.2f}s"


# ===========================================================================
# ops.py _subprocess_timeout() helper tests
#
# _subprocess_timeout() reads the env var at CALL time, so these tests only
# monkeypatch the env. They must never importlib.reload(primer_runtime.ops):
# a reload re-creates OpError, and every module or test that imported the
# old class (server, exec, pty_op, runtime/tests/test_ops.py) then stops
# matching what ops raises, for the rest of the process.
# ===========================================================================


def test_runtime_ops_subprocess_timeout_default(monkeypatch):
    """_subprocess_timeout() returns 120.0 when the env var is absent."""
    monkeypatch.delenv("PRIMER_SUBPROCESS_TIMEOUT_SECONDS", raising=False)
    from primer_runtime.ops import _subprocess_timeout

    assert _subprocess_timeout() == 120.0


def test_runtime_ops_subprocess_timeout_env(monkeypatch):
    """_subprocess_timeout() reads PRIMER_SUBPROCESS_TIMEOUT_SECONDS."""
    monkeypatch.setenv("PRIMER_SUBPROCESS_TIMEOUT_SECONDS", "60.0")
    from primer_runtime.ops import _subprocess_timeout

    assert _subprocess_timeout() == 60.0


def test_runtime_ops_subprocess_timeout_invalid_env(monkeypatch):
    """_subprocess_timeout() falls back to 120.0 when the env var is invalid."""
    monkeypatch.setenv("PRIMER_SUBPROCESS_TIMEOUT_SECONDS", "not_a_float")
    from primer_runtime.ops import _subprocess_timeout

    assert _subprocess_timeout() == 120.0


# ===========================================================================
# ops.py _run_git timeout integration test
# ===========================================================================


@pytest.mark.asyncio
async def test_runtime_ops_run_git_timeout(tmp_path: Path, monkeypatch):
    """_run_git kills the process and raises OpError(EINTERNAL) 'timed out' when the deadline fires.

    The ``git`` it runs is a stand-in on PATH that never finishes (``exec
    sleep 30``). A real ``git init`` finishes in a couple of milliseconds,
    the same order as the 1 ms deadline this test used: whenever git exited
    before the deadline was checked (a fast or briefly descheduled worker),
    _run_git returned normally and the test failed with "DID NOT RAISE".
    """
    import time

    from primer_runtime.ops import OpError, _run_git
    from primer_runtime.protocol import ErrorCode

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_git = bin_dir / "git"
    fake_git.write_text("#!/bin/sh\nexec sleep 30\n")
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("PRIMER_SUBPROCESS_TIMEOUT_SECONDS", "0.2")
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    start = time.monotonic()
    with pytest.raises(OpError) as exc_info:
        await _run_git(str(state_dir), "init", "--initial-branch=main")
    elapsed = time.monotonic() - start

    assert exc_info.value.code == ErrorCode.EINTERNAL
    assert "timed out" in exc_info.value.message.lower()
    # Killed at the deadline, not waited out: the stand-in sleeps for 30 s.
    assert elapsed < 10.0, f"Expected prompt return after kill, took {elapsed:.2f}s"
