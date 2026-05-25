"""Integration smoke test: SandboxStatProbe against a real Docker container.

This test is SKIP-BY-DEFAULT. It only runs when:

    MATRIX_RUN_DOCKER_TESTS=1

is set in the environment, AND the ``docker`` CLI is reachable.

What it covers:
* Spins up a SandboxWorkspace via the container backend against a real
  busybox image.
* Constructs a SandboxStatProbe directly.
* Calls snapshot on a file that exists and one that doesn't.
* Execs ``touch`` inside the container to bump the mtime.
* Snapshots again and asserts mtime changed.
* Tears down with sandbox.remove() + volume cleanup.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from matrix.bus.watcher import SandboxStatProbe


# ---------------------------------------------------------------------------
# Skip gate
# ---------------------------------------------------------------------------

_DOCKER_TESTS_ENABLED = os.environ.get("MATRIX_RUN_DOCKER_TESTS") == "1"
_DOCKER_AVAILABLE = shutil.which("docker") is not None

pytestmark = pytest.mark.skipif(
    not (_DOCKER_TESTS_ENABLED and _DOCKER_AVAILABLE),
    reason=(
        "Docker smoke tests disabled. "
        "Set MATRIX_RUN_DOCKER_TESTS=1 and ensure `docker` is on PATH to enable."
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_sandbox():
    """Create a minimal container sandbox backed by busybox."""
    from matrix.workspace.container.backend import ContainerWorkspaceBackend
    from matrix.model.workspace import WorkspaceTemplate

    template = WorkspaceTemplate(
        id="smoke-test",
        name="Smoke Test",
        image="busybox:latest",
        state_path=".matrix",
        tmp_path=".matrix/tmp",
    )
    backend = ContainerWorkspaceBackend()
    ws = await backend.create(
        workspace_id="smoke-ws",
        template=template,
    )
    return ws


# ---------------------------------------------------------------------------
# The smoke test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sandbox_stat_probe_detects_mtime_change_after_touch():
    """Spin up a busybox container, use SandboxStatProbe to watch a file,
    exec touch inside the container, verify mtime changed.

    Skip cleanly when docker isn't available or MATRIX_RUN_DOCKER_TESTS != 1.
    """
    ws = await _make_sandbox()
    sandbox = ws._sandbox
    workspace_root = ws._workspace_root

    try:
        # Create a test file inside the container.
        await sandbox.exec(
            ["sh", "-c", "echo hello > /workspace/probe_test.txt"],
            workdir=workspace_root,
        )

        probe = SandboxStatProbe(sandbox=sandbox, workspace_root=workspace_root)

        # Initial snapshot — file should exist.
        snap1 = await probe.snapshot(["probe_test.txt", "nonexistent.txt"])
        mtime1, size1, exists1 = snap1["probe_test.txt"]
        assert exists1, "probe_test.txt should exist after creation"
        assert mtime1 is not None

        _, _, exists_ne = snap1["nonexistent.txt"]
        assert not exists_ne, "nonexistent.txt should not exist"

        # Wait ≥1 second so integer-second mtime advances.
        import asyncio
        await asyncio.sleep(1.1)

        # Touch the file to bump mtime.
        await sandbox.exec(
            ["touch", "/workspace/probe_test.txt"],
            workdir=workspace_root,
        )

        # Second snapshot — mtime should have changed.
        snap2 = await probe.snapshot(["probe_test.txt"])
        mtime2, _, exists2 = snap2["probe_test.txt"]
        assert exists2
        assert mtime2 is not None
        assert mtime2 > mtime1, (
            f"Expected mtime2 ({mtime2}) > mtime1 ({mtime1}) after touch"
        )

    finally:
        # Tear down: remove the sandbox + volume.
        try:
            await sandbox.remove()
        except Exception:
            pass
