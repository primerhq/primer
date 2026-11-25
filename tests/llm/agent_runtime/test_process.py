"""The shared agent-runtime child layer: prefix-parameterized temp-dir lifecycle and orphan sweep."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from primer.llm.agent_runtime.process import child_env, child_process
from primer.llm.claude_code.process import sweep_orphans


async def test_child_process_uses_the_given_prefix():
    """The temp directory is named ``<prefix>-<worker pid>-*`` and holds exactly
    the given files, for a prefix other than claude_code's."""
    paths = []

    def argv(tmp: Path) -> list[str]:
        paths.append(tmp)
        return [sys.executable, "-I", "-c", "pass"]

    async with child_process(
        argv,
        env=child_env([]),
        prefix="primer-test-runtime",
        files={"system.md": "s", "extra.txt": "x"},
    ) as proc:
        [tmp] = paths
        assert tmp.name.startswith(f"primer-test-runtime-{os.getpid()}-")
        assert (tmp / "system.md").read_text(encoding="utf-8") == "s"
        assert (tmp / "extra.txt").read_text(encoding="utf-8") == "x"
        assert json.loads((tmp / "child.json").read_text(encoding="utf-8")) == {"pid": proc.pid, "pgid": proc.pid}
        assert tmp.stat().st_mode & 0o777 == 0o700
    assert not tmp.exists()


@pytest.mark.parametrize("name", ["nested/input.txt", "child.json", "stderr.log"])
async def test_invalid_input_names_are_rejected_before_spawn(name, tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    argv = Mock()
    with pytest.raises(ValueError):
        async with child_process(argv, env={}, prefix="primer-test", files={name: "data"}):
            pytest.fail("an invalid input name must not start the child")
    argv.assert_not_called()
    assert list(tmp_path.iterdir()) == []


async def test_temp_directory_prefix_must_be_a_single_component(tmp_path):
    argv = Mock()
    with pytest.raises(ValueError, match="filename component"):
        async with child_process(argv, env={}, prefix=str(tmp_path / "outside")):
            pytest.fail("an invalid prefix must not start the child")
    argv.assert_not_called()
    assert list(tmp_path.iterdir()) == []


async def test_claude_sweep_ignores_other_providers_child_records(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("primer.llm.agent_runtime.process._worker_is_alive", lambda pid: False)
    killpg = Mock()
    monkeypatch.setattr(os, "killpg", killpg)
    cc_dir = tmp_path / "primer-cc-123-x"
    cx_dir = tmp_path / "primer-codex-123-y"
    for d in (cc_dir, cx_dir):
        d.mkdir(mode=0o700)
    record = cx_dir / "child.json"
    pgid = os.getpgrp() + 2
    contents = json.dumps({"pid": pgid, "pgid": pgid})
    record.write_text(contents, encoding="utf-8")

    assert await sweep_orphans(tmp_path) == 1
    assert not cc_dir.exists()
    assert record.read_text(encoding="utf-8") == contents
    killpg.assert_not_called()
