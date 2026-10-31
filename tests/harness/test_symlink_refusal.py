"""A harness repository is its author's: a symlink in it must never make the server read a file outside the checkout (review of knowledge, finding 1).

The template loader (``content_path`` and the template files), the bundle-hash walk of the dispatcher and the metadata fetch's walk all refuse an
entry that is a symlink, or under one, or that resolves outside the checkout, with an error that names the entry. A plain file installs as before.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from primer.harness.dispatch import _collect_bundle_files
from primer.harness.git import HarnessGitError, fetch_harness_metadata
from primer.harness.paths import symlink_reason
from primer.harness.template import HarnessTemplateError, render_bundle

DOC_TEMPLATE = "kind: document\nname: readme\ncontent_path: docs/readme.md\nspec:\n  collection_id: kb\n  path: readme\n"


def _repo(tmp_path, *, link_to=None, body="hello"):
    """A checkout with one document template; ``docs/readme.md`` is a plain file with ``body``, or a symlink to ``link_to``."""
    repo = tmp_path / "repo"
    (repo / "templates").mkdir(parents=True)
    (repo / "docs").mkdir()
    (repo / "templates" / "doc.yaml").write_text(DOC_TEMPLATE)
    if link_to is None:
        (repo / "docs" / "readme.md").write_text(body)
    else:
        os.symlink(link_to, repo / "docs" / "readme.md")
    return repo


async def _render(repo):
    return await render_bundle(checkout_dir=str(repo), subpath=None, overrides={}, harness_ctx={"slug": "acme"})


async def test_a_plain_content_file_still_installs(tmp_path):
    files = await _render(_repo(tmp_path, body="plain body"))
    assert files[0].content == "plain body"


async def test_a_content_path_symlinked_outside_the_checkout_is_refused(tmp_path):
    secret = tmp_path / "server-secret.txt"
    secret.write_text("DATABASE_PASSWORD=hunter2")
    with pytest.raises(HarnessTemplateError) as err:
        await _render(_repo(tmp_path, link_to=secret))
    assert err.value.code == "template_yaml_invalid" and "docs/readme.md" in err.value.message and "symbolic link" in err.value.message
    assert "hunter2" not in str(err.value)


async def test_a_content_path_symlinked_inside_the_checkout_is_refused_too(tmp_path):
    repo = _repo(tmp_path, body="x")
    (repo / "docs" / "real.md").write_text("real")
    (repo / "docs" / "readme.md").unlink()
    os.symlink(repo / "docs" / "real.md", repo / "docs" / "readme.md")
    with pytest.raises(HarnessTemplateError) as err:
        await _render(repo)
    assert "symbolic link" in err.value.message


async def test_a_content_path_under_a_symlinked_directory_is_refused(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "readme.md").write_text("secret")
    repo = _repo(tmp_path)
    (repo / "docs" / "readme.md").unlink()
    (repo / "docs").rmdir()
    os.symlink(outside, repo / "docs")
    with pytest.raises(HarnessTemplateError) as err:
        await _render(repo)
    assert "docs" in err.value.message and "symbolic link" in err.value.message


async def test_a_symlinked_template_file_is_refused(tmp_path):
    secret = tmp_path / "evil.yaml"
    secret.write_text(DOC_TEMPLATE)
    repo = _repo(tmp_path)
    (repo / "templates" / "linked.yaml").symlink_to(secret)
    with pytest.raises(HarnessTemplateError) as err:
        await _render(repo)
    assert err.value.template == "linked.yaml" and "symbolic link" in err.value.message


def test_the_dispatcher_bundle_walk_refuses_a_symlink(tmp_path):
    secret = tmp_path / "big.bin"
    secret.write_bytes(b"x" * 10)
    repo = _repo(tmp_path, link_to=secret)
    with pytest.raises(HarnessGitError) as err:
        _collect_bundle_files(repo)
    assert err.value.code == "harness_symlink_refused" and "docs/readme.md" in err.value.message


def test_the_dispatcher_bundle_walk_still_hashes_plain_files_and_skips_git(tmp_path):
    repo = _repo(tmp_path, body="plain")
    (repo / ".git").mkdir()
    os.symlink("/etc/hostname", repo / ".git" / "link")  # inside .git: never part of the bundle, never refused
    names = [name for name, _ in _collect_bundle_files(repo)]
    assert names == ["docs/readme.md", "templates/doc.yaml"]


async def test_the_metadata_fetch_walk_refuses_a_symlink(tmp_path):
    repo = tmp_path / "git-repo"
    repo.mkdir()
    (repo / "harness.yaml").write_text("apiVersion: primer/v1\nkind: Harness\nmetadata:\n  name: X\n")
    secret = tmp_path / "secret.txt"
    secret.write_text("s")
    os.symlink(secret, repo / "notes.txt")
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init"], check=True, capture_output=True)
    with pytest.raises(HarnessGitError) as err:
        await fetch_harness_metadata(git_url=f"file://{repo}", ref="main", subpath=None, token=None)
    assert err.value.code == "harness_symlink_refused" and "notes.txt" in err.value.message


def test_symlink_reason_names_both_ways_out(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "ok.txt").write_text("a")
    outside = tmp_path / "outside.txt"
    outside.write_text("b")
    os.symlink(outside, root / "out.txt")
    os.symlink(root / "ok.txt", root / "in.txt")
    assert symlink_reason(root, root / "ok.txt") is None
    assert symlink_reason(root, root / "out.txt") == "is a symbolic link"
    assert symlink_reason(root, root / "in.txt") == "is a symbolic link"
