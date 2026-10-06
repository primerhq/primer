"""``tests/_support/scratch_dir.py``: the scratch directory of an ``asyncio.run`` scenario is removed after the loop is shut down.

The old shape (``TemporaryDirectory`` inside the coroutine) removed the directory while a ``to_thread`` write the scenario had
left behind could still be creating files under ``.git``: ``OSError: [Errno 39] Directory not empty: '.git'``.
"""

from __future__ import annotations

import asyncio
import errno
import shutil
import threading
import time
from pathlib import Path

import pytest

from tests._support import scratch_dir
from tests._support.scratch_dir import REMOVE_ATTEMPTS, remove_scratch_dir, run_in_scratch_dir


_REAL_RMTREE = shutil.rmtree  # the patch below replaces ``shutil.rmtree`` itself


def _tree(root: Path) -> Path:
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "b" / "f.txt").write_text("x")
    return root


class _FlakyRmtree:
    """``shutil.rmtree`` that fails ``failures`` times with ``error`` before it really removes the tree."""

    def __init__(self, failures: int, error: OSError) -> None:
        self.failures, self.error, self.calls = failures, error, 0

    def __call__(self, path, *args, **kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return _REAL_RMTREE(path, *args, **kwargs)


@pytest.fixture
def slept(monkeypatch) -> list[float]:
    delays: list[float] = []
    monkeypatch.setattr(scratch_dir.time, "sleep", delays.append)
    return delays


def test_a_tree_is_removed(tmp_path):
    root = _tree(tmp_path / "root")
    remove_scratch_dir(root)
    assert not root.exists()


def test_a_missing_directory_is_not_an_error(tmp_path):
    remove_scratch_dir(tmp_path / "never-made")


def test_enotempty_is_retried_until_the_writer_is_done(tmp_path, monkeypatch, slept):
    root = _tree(tmp_path / "root")
    rmtree = _FlakyRmtree(2, OSError(errno.ENOTEMPTY, "Directory not empty", ".git"))
    monkeypatch.setattr(scratch_dir.shutil, "rmtree", rmtree)
    remove_scratch_dir(root)
    assert not root.exists() and rmtree.calls == 3 and len(slept) == 2


def test_enotempty_that_never_clears_is_raised_after_the_attempts(tmp_path, monkeypatch, slept):
    root = _tree(tmp_path / "root")
    rmtree = _FlakyRmtree(10**6, OSError(errno.ENOTEMPTY, "Directory not empty", ".git"))
    monkeypatch.setattr(scratch_dir.shutil, "rmtree", rmtree)
    with pytest.raises(OSError) as raised:
        remove_scratch_dir(root)
    assert raised.value.errno == errno.ENOTEMPTY
    assert rmtree.calls == REMOVE_ATTEMPTS and len(slept) == REMOVE_ATTEMPTS - 1


def test_any_other_error_is_raised_at_once(tmp_path, monkeypatch, slept):
    root = _tree(tmp_path / "root")
    rmtree = _FlakyRmtree(10**6, PermissionError(errno.EACCES, "Permission denied", "x"))
    monkeypatch.setattr(scratch_dir.shutil, "rmtree", rmtree)
    with pytest.raises(PermissionError):
        remove_scratch_dir(root)
    assert rmtree.calls == 1 and slept == []


def test_the_scenario_gets_a_directory_that_exists_and_its_result_comes_back():
    seen: dict[str, Path] = {}

    async def scenario(root: Path) -> str:
        seen["root"] = root
        assert root.is_dir() and root.name.startswith("probe-")
        (root / "f").write_text("x")
        return "result"

    assert run_in_scratch_dir(scenario, prefix="probe-") == "result"
    assert not seen["root"].exists()


def test_a_scenario_that_raises_still_removes_the_directory_and_propagates():
    seen: dict[str, Path] = {}

    async def scenario(root: Path) -> None:
        seen["root"] = root
        raise ValueError("the scenario failed")

    with pytest.raises(ValueError, match="the scenario failed"):
        run_in_scratch_dir(scenario, prefix="probe-")
    assert not seen["root"].exists()


def test_the_directory_is_removed_only_after_a_thread_the_scenario_left_behind_has_finished(monkeypatch):
    """The ordering the fix is about: the scenario returns while a ``to_thread`` job is still running (it cannot be
    cancelled); ``asyncio.run`` waits for it when it shuts the loop down, and only then is the directory removed."""
    order: list[str] = []
    started = threading.Event()
    real_remove = scratch_dir.remove_scratch_dir

    def remove(path):
        order.append("removed")
        real_remove(path)

    monkeypatch.setattr(scratch_dir, "remove_scratch_dir", remove)

    async def scenario(root: Path) -> None:
        def job() -> None:
            started.set()
            time.sleep(0.2)
            (root / "late.txt").write_text("a write that outlives the scenario")
            order.append("written")

        asyncio.create_task(asyncio.to_thread(job))  # deliberately not awaited
        await asyncio.wait_for(_until(started.is_set), timeout=5)

    async def _until(condition) -> None:
        while not condition():
            await asyncio.sleep(0.005)

    seen: dict[str, Path] = {}
    original = scenario

    async def recording(root: Path) -> None:
        seen["root"] = root
        await original(root)

    run_in_scratch_dir(recording, prefix="probe-")
    assert order == ["written", "removed"]
    assert not seen["root"].exists()
