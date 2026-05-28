"""Tests for primer.harness.git — uses local bare repos via file:// for cloneable URLs."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from primer.harness.git import (
    HarnessGitError,
    clone_at_ref,
    ls_remote,
    _inject_token,
    _redact,
)


@pytest.fixture
def local_bare_repo(tmp_path) -> str:
    """Create a tiny bare repo with a commit; return its file:// URL."""
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=work, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=work, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=work, check=True)
    (work / "README.md").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=work, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=work, check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=work, check=True)
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True)
    return f"file://{bare}"


def test_inject_token_https():
    out = _inject_token("https://github.com/a/b", "tk")
    assert out == "https://oauth2:tk@github.com/a/b"


def test_inject_token_skips_non_https():
    out = _inject_token("file:///tmp/x", "tk")
    assert out == "file:///tmp/x"


def test_redact_strips_token():
    msg = "failed to clone https://oauth2:supersecret@host/p"
    out = _redact(msg)
    assert "supersecret" not in out
    assert "oauth2:***" in out


async def test_ls_remote_resolves_branch(local_bare_repo):
    sha = await ls_remote(local_bare_repo, token=None, ref="main")
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)


async def test_ls_remote_missing_ref(local_bare_repo):
    with pytest.raises(HarnessGitError) as ei:
        await ls_remote(local_bare_repo, token=None, ref="does-not-exist")
    assert ei.value.code == "ref_not_found"


async def test_clone_at_ref(local_bare_repo, tmp_path):
    dest = tmp_path / "clone"
    await clone_at_ref(local_bare_repo, token=None, ref="main", dest=str(dest))
    assert (dest / "README.md").read_text() == "hello"


async def test_clone_at_sha(local_bare_repo, tmp_path):
    sha = await ls_remote(local_bare_repo, token=None, ref="main")
    dest = tmp_path / "clone"
    await clone_at_ref(local_bare_repo, token=None, ref=sha, dest=str(dest))
    assert (dest / "README.md").exists()


async def test_clone_unreachable_url_fails_safely(tmp_path):
    with pytest.raises(HarnessGitError) as ei:
        await clone_at_ref(
            "https://nonexistent.example.invalid/foo",
            token="should-not-leak",
            ref="main",
            dest=str(tmp_path / "x"),
        )
    # Error message must not contain the token
    assert "should-not-leak" not in str(ei.value)


def test_redact_strips_bare_token_when_provided():
    """Even if git emits the token outside the oauth2:...@ prefix
    (e.g., from a credential-helper log), passing the known token
    strips it from the message."""
    msg = "git complained: ghp_LIVE_TOKEN_xyz is invalid"
    out = _redact(msg, "ghp_LIVE_TOKEN_xyz")
    assert "ghp_LIVE_TOKEN_xyz" not in out
    assert "***" in out


def test_redact_without_token_uses_pattern_only():
    """_redact called without a token still strips the injected prefix."""
    msg = "fatal: oauth2:secretval@host/p"
    out = _redact(msg)
    assert "secretval" not in out
    assert "oauth2:***@" in out
