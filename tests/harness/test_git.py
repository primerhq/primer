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
    _prepare_remote,
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


def test_prepare_remote_strips_the_credential_from_the_url_and_scopes_the_helper_to_the_origin():
    url, env = _prepare_remote("https://github.com:8443/a/b", "tk")
    assert url == "https://github.com:8443/a/b"
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert env["GIT_CONFIG_KEY_0"] == "credential.helper" and env["GIT_CONFIG_VALUE_0"] == ""
    assert env["GIT_CONFIG_KEY_1"] == "credential.https://github.com:8443.helper"
    assert env["PRIMER_GIT_CREDENTIAL_USERNAME"] == "oauth2" and env["PRIMER_GIT_CREDENTIAL_PASSWORD"] == "tk"
    assert "tk" not in env["GIT_CONFIG_VALUE_1"], "the helper reads the secret from the environment; it does not hold it"


def test_prepare_remote_moves_a_url_credential_into_the_environment_and_the_token_wins():
    url, env = _prepare_remote("https://ghp_abc@github.com/a/b", None)
    assert url == "https://github.com/a/b"
    assert (env["PRIMER_GIT_CREDENTIAL_USERNAME"], env["PRIMER_GIT_CREDENTIAL_PASSWORD"]) == ("ghp_abc", "")
    url, env = _prepare_remote("https://me:p%40ss@github.com/a/b", None)
    assert url == "https://github.com/a/b"
    assert (env["PRIMER_GIT_CREDENTIAL_USERNAME"], env["PRIMER_GIT_CREDENTIAL_PASSWORD"]) == ("me", "p@ss")
    url, env = _prepare_remote("https://ghp_abc@github.com/a/b", "stored")
    assert url == "https://github.com/a/b"
    assert (env["PRIMER_GIT_CREDENTIAL_USERNAME"], env["PRIMER_GIT_CREDENTIAL_PASSWORD"]) == ("oauth2", "stored")


def test_prepare_remote_sends_nothing_without_a_credential_or_over_a_non_https_url():
    assert _prepare_remote("file:///tmp/x", "tk") == ("file:///tmp/x", {})
    assert _prepare_remote("https://github.com/a/b", None) == ("https://github.com/a/b", {})


def test_prepare_remote_appends_to_config_entries_already_in_the_environment(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    _, env = _prepare_remote("https://h.example/r", "tk")
    assert env["GIT_CONFIG_COUNT"] == "4"
    assert "GIT_CONFIG_KEY_2" in env and "GIT_CONFIG_KEY_0" not in env


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
