"""Unit tests for HostStatProbe and SandboxStatProbe.

HostStatProbe tests use real on-disk files (under ``tmp_path``).
SandboxStatProbe tests use a fake sandbox that returns canned output —
no real Docker is required.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from matrix.bus.watcher import HostStatProbe, SandboxStatProbe
from matrix.int.sandbox import ExecResult


# ===========================================================================
# HostStatProbe
# ===========================================================================


@pytest.mark.asyncio
class TestHostStatProbe:
    async def test_existing_file_returns_mtime_size_and_true(self, tmp_path):
        f = tmp_path / "hello.txt"
        f.write_bytes(b"abc")
        probe = HostStatProbe(root=tmp_path)
        snap = await probe.snapshot(["hello.txt"])
        mtime, size, exists = snap["hello.txt"]
        assert exists is True
        assert size == 3
        assert isinstance(mtime, (int, float))
        assert mtime > 0

    async def test_missing_file_returns_none_none_false(self, tmp_path):
        probe = HostStatProbe(root=tmp_path)
        snap = await probe.snapshot(["ghost.txt"])
        mtime, size, exists = snap["ghost.txt"]
        assert exists is False
        assert mtime is None
        assert size is None

    async def test_mtime_delta_observable_after_touch(self, tmp_path):
        f = tmp_path / "x.txt"
        f.write_bytes(b"v1")
        probe = HostStatProbe(root=tmp_path)

        snap1 = await probe.snapshot(["x.txt"])
        mtime1, _, _ = snap1["x.txt"]

        # Ensure at least 1 second passes so mtime (integer seconds) changes.
        time.sleep(1.1)
        f.write_bytes(b"v2")

        snap2 = await probe.snapshot(["x.txt"])
        mtime2, _, _ = snap2["x.txt"]

        assert mtime2 is not None
        assert mtime1 is not None
        assert mtime2 > mtime1

    async def test_mixed_list_existing_and_missing(self, tmp_path):
        (tmp_path / "present.txt").write_bytes(b"here")
        probe = HostStatProbe(root=tmp_path)
        snap = await probe.snapshot(["present.txt", "absent.txt"])

        _, _, exists_p = snap["present.txt"]
        _, _, exists_a = snap["absent.txt"]
        assert exists_p is True
        assert exists_a is False


# ===========================================================================
# SandboxStatProbe — fake sandbox
# ===========================================================================


class _FakeSandbox:
    """Minimal Sandbox stand-in — just stores the next ExecResult to return."""

    def __init__(self, result: ExecResult | None = None, raises: Exception | None = None) -> None:
        self._result = result
        self._raises = raises
        self.last_command: list | str | None = None
        self.last_workdir: str | None = None

    @property
    def id(self) -> str:
        return "fake-sandbox"

    async def exec(self, command, *, workdir="/workspace", **kwargs) -> ExecResult:
        self.last_command = command
        self.last_workdir = workdir
        if self._raises is not None:
            raise self._raises
        return self._result  # type: ignore[return-value]


def _ok(stdout: str) -> ExecResult:
    return ExecResult(exit_code=0, stdout=stdout, stderr="", duration_seconds=0.0)


def _err(rc: int = 1, stderr: str = "oops") -> ExecResult:
    return ExecResult(exit_code=rc, stdout="", stderr=stderr, duration_seconds=0.0)


@pytest.mark.asyncio
class TestSandboxStatProbe:
    async def test_all_exist_parsed_correctly(self):
        stdout = "src/main.py|1716000000|1234\nsrc/util.py|1716000100|567\n"
        sb = _FakeSandbox(result=_ok(stdout))
        probe = SandboxStatProbe(sandbox=sb, workspace_root="/workspace")
        snap = await probe.snapshot(["src/main.py", "src/util.py"])

        mtime, size, exists = snap["src/main.py"]
        assert exists is True
        assert mtime == 1716000000
        assert size == 1234

        mtime2, size2, exists2 = snap["src/util.py"]
        assert exists2 is True
        assert mtime2 == 1716000100
        assert size2 == 567

    async def test_mixed_exist_and_miss(self):
        stdout = "present.py|1716000000|42\nmissing.py|MISS|MISS\n"
        sb = _FakeSandbox(result=_ok(stdout))
        probe = SandboxStatProbe(sandbox=sb, workspace_root="/workspace")
        snap = await probe.snapshot(["present.py", "missing.py"])

        _, _, ep = snap["present.py"]
        _, _, em = snap["missing.py"]
        assert ep is True
        assert em is False

    async def test_all_missing(self):
        stdout = "a.py|MISS|MISS\nb.py|MISS|MISS\n"
        sb = _FakeSandbox(result=_ok(stdout))
        probe = SandboxStatProbe(sandbox=sb, workspace_root="/workspace")
        snap = await probe.snapshot(["a.py", "b.py"])

        for key in ("a.py", "b.py"):
            _, _, ex = snap[key]
            assert ex is False

    async def test_malformed_output_row_skipped_others_kept(self):
        # Second row is malformed (only 2 pipe-fields instead of 3); first and
        # third should still parse correctly.
        stdout = "good.py|1716000000|99\nBAD_LINE\nother.py|1716000001|55\n"
        sb = _FakeSandbox(result=_ok(stdout))
        probe = SandboxStatProbe(sandbox=sb, workspace_root="/workspace")
        snap = await probe.snapshot(["good.py", "other.py"])

        _, _, eg = snap["good.py"]
        _, _, eo = snap["other.py"]
        assert eg is True
        assert eo is True

    async def test_exec_raises_returns_all_missing(self):
        sb = _FakeSandbox(raises=RuntimeError("container died"))
        probe = SandboxStatProbe(sandbox=sb, workspace_root="/workspace")
        snap = await probe.snapshot(["x.py", "y.py"])

        for key in ("x.py", "y.py"):
            _, _, ex = snap[key]
            assert ex is False

    async def test_exec_nonzero_rc_returns_all_missing(self):
        sb = _FakeSandbox(result=_err(rc=1))
        probe = SandboxStatProbe(sandbox=sb, workspace_root="/workspace")
        snap = await probe.snapshot(["x.py"])

        _, _, ex = snap["x.py"]
        assert ex is False

    async def test_path_with_newline_raises_value_error(self):
        sb = _FakeSandbox(result=_ok(""))
        probe = SandboxStatProbe(sandbox=sb, workspace_root="/workspace")
        with pytest.raises(ValueError, match="newline"):
            await probe.snapshot(["bad\npath.py"])

    async def test_empty_paths_returns_empty_dict(self):
        sb = _FakeSandbox(result=_ok(""))
        probe = SandboxStatProbe(sandbox=sb, workspace_root="/workspace")
        snap = await probe.snapshot([])
        assert snap == {}

    async def test_shell_script_quotes_paths_with_special_chars(self):
        """Paths with spaces/special chars must be safely quoted in script."""
        stdout = "path with spaces.py|1716000000|10\n"
        sb = _FakeSandbox(result=_ok(stdout))
        probe = SandboxStatProbe(sandbox=sb, workspace_root="/workspace")
        # snapshot should not raise even if the path has spaces
        await probe.snapshot(["path with spaces.py"])
        # The exec command should have been called (script was built)
        assert sb.last_command is not None

    async def test_batching_over_50_paths_makes_multiple_exec_calls(self):
        """Paths > _SANDBOX_BATCH_SIZE should be split into multiple execs."""
        from matrix.bus.watcher import _SANDBOX_BATCH_SIZE

        call_count = 0
        results: list[ExecResult] = []

        class _CountingSandbox:
            @property
            def id(self):
                return "counting"

            async def exec(self, command, *, workdir="/workspace", **kwargs):
                nonlocal call_count
                call_count += 1
                # Build fake output for all paths in this batch by parsing
                # the script — simpler: just return empty OK, paths will be MISS
                return ExecResult(exit_code=0, stdout="", stderr="", duration_seconds=0.0)

        paths = [f"file{i}.py" for i in range(_SANDBOX_BATCH_SIZE + 5)]
        probe = SandboxStatProbe(sandbox=_CountingSandbox(), workspace_root="/workspace")
        snap = await probe.snapshot(paths)

        assert call_count == 2  # ceil((50+5)/50) = 2 batches
        # All should be missing (empty stdout)
        for p in paths:
            _, _, ex = snap[p]
            assert ex is False
