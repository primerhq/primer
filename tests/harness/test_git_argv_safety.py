"""git_url and ref never reach git as options, and git runs with only the allowed transports (AUTHZ-04 / INJ-03 / FS-02).

Every git call in :mod:`primer.harness.git` is inspected through a fake ``create_subprocess_exec`` that records the argv and env:
no real git, no network. A bad git_url or ref is refused before any process starts.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from primer.harness import git as git_mod
from primer.harness.git import HarnessGitError, clone_at_ref, ls_remote, push_bundle

_SHA = "0123456789abcdef0123456789abcdef01234567"
_URL = "https://github.com/example/repo"


class _FakeProc:
    def __init__(self, stdout: bytes) -> None:
        self.returncode = 0
        self._stdout = stdout

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, b""

    def kill(self) -> None:  # pragma: no cover - never times out here
        pass


@pytest.fixture
def calls(monkeypatch) -> list[dict[str, Any]]:
    recorded: list[dict[str, Any]] = []

    async def fake_exec(*argv: str, **kwargs: Any) -> _FakeProc:
        recorded.append({"argv": list(argv), "env": kwargs.get("env")})
        out = f"{_SHA}\trefs/heads/main\n".encode() if "ls-remote" in argv else b""
        return _FakeProc(out)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.delenv("PRIMER_HARNESS_ALLOW_FILE_URLS", raising=False)
    return recorded


def _assert_hardened(call: dict[str, Any]) -> None:
    argv = call["argv"]
    assert argv[0] == "git"
    joined = " ".join(argv)
    assert "-c protocol.allow=never" in joined, argv
    assert "-c protocol.https.allow=always" in joined, argv
    assert call["env"] is not None and call["env"].get("GIT_PROTOCOL_FROM_USER") == "0", argv


def _assert_url_after_separator(call: dict[str, Any], url_fragment: str) -> None:
    argv = call["argv"]
    url_at = next(i for i, a in enumerate(argv) if url_fragment in a)
    assert "--" in argv[:url_at], f"no '--' before the url in {argv}"


async def test_ls_remote_puts_the_url_and_ref_after_a_separator(calls):
    assert await ls_remote(_URL, token=None, ref="main") == _SHA
    (call,) = calls
    _assert_hardened(call)
    _assert_url_after_separator(call, "github.com/example/repo")
    assert call["argv"][-2:] == [_URL, "main"]


async def test_clone_of_a_branch_puts_the_url_after_a_separator(calls, tmp_path):
    await clone_at_ref(_URL, token="tk", ref="main", dest=str(tmp_path / "d"))
    (call,) = calls
    _assert_hardened(call)
    _assert_url_after_separator(call, "github.com/example/repo")
    assert "--branch=main" in call["argv"] or call["argv"][call["argv"].index("--branch") + 1] == "main"


async def test_clone_of_a_sha_puts_the_url_and_sha_after_a_separator(calls, tmp_path):
    await clone_at_ref(_URL, token=None, ref=_SHA, dest=str(tmp_path / "d"))
    assert len(calls) == 3  # init, fetch, checkout
    for call in calls:
        _assert_hardened(call)
    fetch = calls[1]["argv"]
    _assert_url_after_separator(calls[1], "github.com/example/repo")
    assert fetch[-2:] == [_URL, _SHA]


async def test_push_puts_every_url_and_ref_after_a_separator(calls, tmp_path):
    await push_bundle(
        url=_URL, token=None, ref="main", files=[("a.txt", b"x")], subpath=None,
        commit_message="m", expected_remote_sha=None,
    )
    assert calls, "push ran no git"
    for call in calls:
        _assert_hardened(call)
    clone = next(c for c in calls if "clone" in c["argv"])
    _assert_url_after_separator(clone, "github.com/example/repo")
    push = next(c for c in calls if "push" in c["argv"])
    assert push["argv"][push["argv"].index("push") + 1] == "--", push["argv"]


@pytest.mark.parametrize(
    "url",
    [
        "--upload-pack=touch /tmp/pwned",
        "-oProxyCommand=touch /tmp/pwned",
        "ext::sh -c id",
        "file:///etc",
        "/etc",
        "http://example.com/repo",
        "https://",
        "ssh://git@example.com/repo",
    ],
)
async def test_an_unsafe_url_is_refused_before_git_starts(calls, tmp_path, url):
    with pytest.raises(HarnessGitError) as ls_exc:
        await ls_remote(url, token=None, ref="main")
    with pytest.raises(HarnessGitError):
        await clone_at_ref(url, token=None, ref="main", dest=str(tmp_path / "d"))
    with pytest.raises(HarnessGitError):
        await push_bundle(
            url=url, token=None, ref="main", files=[], subpath=None, commit_message="m", expected_remote_sha=None,
        )
    assert ls_exc.value.code == "invalid_git_url"
    assert calls == [], "git started for an unsafe url"


@pytest.mark.parametrize("ref", ["--output=x", "-b", "main..x", "a b", "@{-1}", "x;id"])
async def test_an_unsafe_ref_is_refused_before_git_starts(calls, tmp_path, ref):
    with pytest.raises(HarnessGitError) as ls_exc:
        await ls_remote(_URL, token=None, ref=ref)
    with pytest.raises(HarnessGitError):
        await clone_at_ref(_URL, token=None, ref=ref, dest=str(tmp_path / "d"))
    with pytest.raises(HarnessGitError):
        await push_bundle(
            url=_URL, token=None, ref=ref, files=[], subpath=None, commit_message="m", expected_remote_sha=None,
        )
    assert ls_exc.value.code == "invalid_git_ref"
    assert calls == [], "git started for an unsafe ref"


async def test_file_urls_need_the_operator_opt_in(calls, monkeypatch, tmp_path):
    with pytest.raises(HarnessGitError):
        await ls_remote("file:///srv/repo.git", token=None, ref="main")
    assert calls == []

    monkeypatch.setenv("PRIMER_HARNESS_ALLOW_FILE_URLS", "1")
    await ls_remote("file:///srv/repo.git", token=None, ref="main")
    (call,) = calls
    assert "-c protocol.file.allow=always" in " ".join(call["argv"])
    _assert_url_after_separator(call, "file:///srv/repo.git")


def test_the_dependency_ref_model_refuses_unsafe_urls_and_refs(monkeypatch):
    from pydantic import ValidationError

    from primer.model.harness import DependencyRef

    monkeypatch.delenv("PRIMER_HARNESS_ALLOW_FILE_URLS", raising=False)
    for url in ["--upload-pack=x", "ext::sh -c id", "file:///etc", "/etc"]:
        with pytest.raises(ValidationError):
            DependencyRef(name="dep", git_url=url)
    with pytest.raises(ValidationError):
        DependencyRef(name="dep", git_url=_URL, ref="--output=x")
    assert DependencyRef(name="dep", git_url=_URL, ref="v1.0").ref == "v1.0"


def test_git_module_reads_the_opt_in_at_call_time(monkeypatch):
    # The env var is read per call, not cached at import, so an operator restart is the only thing that changes it.
    monkeypatch.setenv("PRIMER_HARNESS_ALLOW_FILE_URLS", "1")
    assert git_mod.validate_git_url("file:///srv/repo.git") == "file:///srv/repo.git"
    monkeypatch.delenv("PRIMER_HARNESS_ALLOW_FILE_URLS")
    with pytest.raises(ValueError):
        git_mod.validate_git_url("file:///srv/repo.git")
