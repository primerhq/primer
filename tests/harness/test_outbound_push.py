"""push_bundle against a file-protocol bare repo — Spec B §6, §10."""

from __future__ import annotations

import subprocess

import pytest

from primer.harness.git import HarnessGitError, push_bundle


def _init_bare(tmp_path):
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)],
        check=True, capture_output=True,
    )
    return bare


def _seed_bare_with_file(tmp_path, bare, rel: str, content: str) -> str:
    """Seed ``bare`` with a single commit containing ``rel`` and return its SHA."""
    seed = tmp_path / "seed"
    subprocess.run(
        ["git", "clone", f"file://{bare}", str(seed)],
        check=True, capture_output=True,
    )
    target = seed / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    subprocess.run(
        ["git", "-C", str(seed), "add", "."],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(seed),
         "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-m", "init"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(seed), "push", "origin", "main"],
        check=True, capture_output=True,
    )
    out = subprocess.run(
        ["git", "-C", str(seed), "rev-parse", "HEAD"],
        check=True, capture_output=True,
    )
    return out.stdout.decode().strip()


@pytest.mark.asyncio
async def test_push_bundle_writes_commits_pushes(tmp_path):
    bare = _init_bare(tmp_path)
    files = [
        ("harness.yaml", b"apiVersion: primer/v1\nkind: Harness\nmetadata:\n  name: X\n"),
        ("overrides.schema.json", b'{"type": "object", "properties": {}}'),
        ("templates/assistant.yaml", b"kind: agent\nname: assistant\nspec: {}\n"),
    ]
    new_sha = await push_bundle(
        url=f"file://{bare}", token=None, ref="main",
        files=files, subpath=None,
        commit_message="primer outbound test",
        expected_remote_sha=None,
    )
    assert len(new_sha) == 40

    # Clone the bare repo back and verify the files made it.
    work = tmp_path / "work"
    subprocess.run(
        ["git", "clone", f"file://{bare}", str(work)],
        check=True, capture_output=True,
    )
    assert (work / "harness.yaml").read_bytes().startswith(b"apiVersion: primer/v1")
    assert (work / "overrides.schema.json").is_file()
    assert (work / "templates" / "assistant.yaml").is_file()


@pytest.mark.asyncio
async def test_push_refuses_when_remote_diverged(tmp_path):
    bare = _init_bare(tmp_path)
    _seed_bare_with_file(tmp_path, bare, "x.txt", "a")
    with pytest.raises(HarnessGitError) as exc:
        await push_bundle(
            url=f"file://{bare}", token=None, ref="main",
            files=[("x.txt", b"b")],
            subpath=None,
            commit_message="x",
            expected_remote_sha="0" * 40,
        )
    assert exc.value.code == "push_remote_diverged"


@pytest.mark.asyncio
async def test_push_noop_when_no_changes(tmp_path):
    bare = _init_bare(tmp_path)
    files = [
        ("harness.yaml", b"apiVersion: primer/v1\nkind: Harness\nmetadata:\n  name: X\n"),
        ("overrides.schema.json", b'{"type": "object", "properties": {}}'),
    ]
    first_sha = await push_bundle(
        url=f"file://{bare}", token=None, ref="main",
        files=files, subpath=None,
        commit_message="initial",
        expected_remote_sha=None,
    )
    assert len(first_sha) == 40

    # Re-push the exact same bundle — should detect no-op and return HEAD
    # without creating a second commit.
    second_sha = await push_bundle(
        url=f"file://{bare}", token=None, ref="main",
        files=files, subpath=None,
        commit_message="no-op",
        expected_remote_sha=first_sha,
    )
    assert second_sha == first_sha

    # Confirm via a fresh clone that only one commit exists.
    work = tmp_path / "verify"
    subprocess.run(
        ["git", "clone", f"file://{bare}", str(work)],
        check=True, capture_output=True,
    )
    log = subprocess.run(
        ["git", "-C", str(work), "log", "--oneline"],
        check=True, capture_output=True,
    )
    assert len(log.stdout.decode().strip().splitlines()) == 1


@pytest.mark.asyncio
async def test_push_with_subpath_writes_under_subpath(tmp_path):
    bare = _init_bare(tmp_path)
    files = [
        ("harness.yaml", b"apiVersion: primer/v1\nkind: Harness\nmetadata:\n  name: S\n"),
        ("templates/assistant.yaml", b"kind: agent\n"),
    ]
    new_sha = await push_bundle(
        url=f"file://{bare}", token=None, ref="main",
        files=files, subpath="charts/x",
        commit_message="sub",
        expected_remote_sha=None,
    )
    assert len(new_sha) == 40

    work = tmp_path / "work"
    subprocess.run(
        ["git", "clone", f"file://{bare}", str(work)],
        check=True, capture_output=True,
    )
    assert (work / "charts" / "x" / "harness.yaml").is_file()
    assert (work / "charts" / "x" / "templates" / "assistant.yaml").is_file()
    # Nothing at the repo root that shouldn't be there.
    assert not (work / "harness.yaml").exists()


@pytest.mark.asyncio
async def test_push_token_redacted_in_errors(tmp_path):
    # Use a non-existent file:// URL so git fails fast.
    bogus = tmp_path / "nope.git"
    token = "ghp_SUPERSECRETTOKEN1234567890"
    with pytest.raises(HarnessGitError) as exc:
        await push_bundle(
            url=f"file://{bogus}", token=token, ref="main",
            files=[("a.txt", b"hi")],
            subpath=None,
            commit_message="x",
            expected_remote_sha=None,
        )
    assert token not in str(exc.value)
    assert token not in exc.value.message
