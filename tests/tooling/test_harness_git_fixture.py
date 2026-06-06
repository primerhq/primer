"""Validate the local bare-git harness bundle builder."""
from __future__ import annotations

import subprocess
from pathlib import Path

from tests._support.harness_git import build_harness_repo, empty_remote


def _clone(url: str, dest: Path) -> Path:
    subprocess.run(["git", "clone", "-q", url, str(dest)], check=True, capture_output=True)
    return dest


def test_build_repo_has_bundle(tmp_path):
    url = build_harness_repo(tmp_path / "h", name="demo-harness")
    assert url.startswith("file://")
    checkout = _clone(url, tmp_path / "checkout")
    manifest = (checkout / "harness.yaml").read_text(encoding="utf-8")
    assert "kind: Harness" in manifest
    assert "name: demo-harness" in manifest
    tpls = {p.name for p in (checkout / "templates").glob("*.yaml")}
    assert tpls == {"assistant.yaml", "kb.yaml", "flow.yaml"}


def test_commit_present(tmp_path):
    url = build_harness_repo(tmp_path / "h")
    checkout = _clone(url, tmp_path / "checkout")
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=str(checkout), check=True, capture_output=True, text=True
    ).stdout
    assert "initial harness bundle" in log


def test_empty_remote_is_pushable(tmp_path):
    url = empty_remote(tmp_path / "out")
    assert url.startswith("file://")
    # cloning an empty bare repo succeeds (no commits yet)
    _clone(url, tmp_path / "empty-checkout")
