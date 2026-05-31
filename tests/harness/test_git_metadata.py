"""fetch_harness_metadata against a file-protocol bare repo — Spec A §11."""

from __future__ import annotations

import json
import subprocess

import pytest

from primer.harness.git import fetch_harness_metadata, HarnessGitError


def _init_repo_with_files(repo_dir, files: dict[str, str]):
    repo_dir.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        target = repo_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    subprocess.run(["git", "init", "-b", "main", str(repo_dir)], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "add", "."], check=True,
                   capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo_dir),
         "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-m", "init"],
        check=True, capture_output=True,
    )


@pytest.mark.asyncio
async def test_fetch_metadata_reads_yaml_and_schema(tmp_path):
    repo = tmp_path / "repo"
    _init_repo_with_files(repo, {
        "harness.yaml": "apiVersion: primer/v1\nkind: Harness\nmetadata:\n  name: X\n",
        "overrides.schema.json": json.dumps({"type": "object", "properties": {}}),
    })
    y, schema, bundle_hash, commit = await fetch_harness_metadata(
        git_url=f"file://{repo}", ref="main", subpath=None, token=None,
    )
    assert y["metadata"]["name"] == "X"
    assert schema["type"] == "object"
    assert len(commit) == 40
    assert len(bundle_hash) == 64


@pytest.mark.asyncio
async def test_fetch_metadata_with_subpath(tmp_path):
    repo = tmp_path / "repo"
    _init_repo_with_files(repo, {
        "charts/sub/harness.yaml": "apiVersion: primer/v1\nkind: Harness\nmetadata:\n  name: Y\n",
        "charts/sub/overrides.schema.json": json.dumps({"type": "object", "properties": {"a": {"type": "string"}}}),
        "other/file.txt": "should not affect bundle hash of the sub",
    })
    y, schema, bundle_hash, _ = await fetch_harness_metadata(
        git_url=f"file://{repo}", ref="main", subpath="charts/sub", token=None,
    )
    assert y["metadata"]["name"] == "Y"
    assert "a" in schema["properties"]


@pytest.mark.asyncio
async def test_fetch_metadata_missing_harness_yaml_errors(tmp_path):
    repo = tmp_path / "repo"
    _init_repo_with_files(repo, {
        "README.md": "no harness.yaml here",
    })
    with pytest.raises(HarnessGitError) as exc:
        await fetch_harness_metadata(
            git_url=f"file://{repo}", ref="main", subpath=None, token=None,
        )
    assert exc.value.code == "dependency_yaml_invalid"


@pytest.mark.asyncio
async def test_fetch_metadata_defaults_empty_overrides_schema(tmp_path):
    repo = tmp_path / "repo"
    _init_repo_with_files(repo, {
        "harness.yaml": "apiVersion: primer/v1\nkind: Harness\nmetadata:\n  name: Z\n",
    })
    y, schema, _, _ = await fetch_harness_metadata(
        git_url=f"file://{repo}", ref="main", subpath=None, token=None,
    )
    assert schema == {"type": "object", "properties": {}}
