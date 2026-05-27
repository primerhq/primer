"""Git client wrapper for harness fetch/clone operations.

We shell out to `git` because pure-Python git libs add a heavy dep and
shelling is universal. Token redaction is done before any error surface.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from typing import Final
from urllib.parse import urlparse, urlunparse


class HarnessGitError(Exception):
    """Raised for any git-related failure with a stable error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


_GIT_TIMEOUT_SECONDS: Final = 300.0


def _inject_token(url: str, token: str | None) -> str:
    """Embed an OAuth2 token in an HTTPS URL.

    Non-HTTPS URLs (e.g. file://) are returned unchanged — used in tests.
    """
    if token is None:
        return url
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return url
    netloc = f"oauth2:{token}@{parsed.hostname or ''}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


_TOKEN_PATTERN = re.compile(r"oauth2:[^@\s]+@")


def _redact(text: str) -> str:
    return _TOKEN_PATTERN.sub("oauth2:***@", text)


def _run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            args,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
            **kwargs,
        )
    except subprocess.TimeoutExpired as exc:
        raise HarnessGitError("subprocess_error", "git command timed out") from exc
    except FileNotFoundError as exc:
        raise HarnessGitError(
            "subprocess_error", "git binary not found on PATH",
        ) from exc


def ls_remote(url: str, *, token: str | None, ref: str) -> str:
    """Return the commit SHA pointed to by ``ref`` on the remote.

    Accepts branches, tags, and full SHAs (the SHA case skips network
    and returns the input).
    """
    if len(ref) == 40 and all(c in "0123456789abcdef" for c in ref):
        # full SHA — no need to ls-remote
        return ref
    effective = _inject_token(url, token)
    proc = _run(["git", "ls-remote", effective, ref])
    if proc.returncode != 0:
        raise HarnessGitError(
            "git_clone_failed" if "Authentication" in (proc.stderr or "") else "ref_not_found",
            _redact((proc.stderr or "").strip() or "ls-remote failed"),
        )
    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if not lines:
        raise HarnessGitError(
            "ref_not_found", f"ref {ref!r} not found on remote",
        )
    sha = lines[0].split("\t", 1)[0].strip()
    if len(sha) != 40:
        raise HarnessGitError("ref_not_found", "could not parse ls-remote output")
    return sha


def clone_at_ref(
    url: str,
    *,
    token: str | None,
    ref: str,
    dest: str,
) -> None:
    """Shallow-clone ``url`` at ``ref`` into ``dest``.

    Handles symbolic refs (branch/tag) via ``--branch=<ref>`` and
    SHA refs via ``init + fetch + checkout``.
    """
    effective = _inject_token(url, token)
    is_sha = len(ref) == 40 and all(c in "0123456789abcdef" for c in ref)
    if is_sha:
        # SHA path: init empty, fetch the specific SHA, checkout.
        proc = _run(["git", "init", "-q", dest])
        if proc.returncode != 0:
            raise HarnessGitError(
                "git_clone_failed",
                _redact((proc.stderr or "git init failed").strip()),
            )
        proc = _run(
            ["git", "fetch", "--depth=1", effective, ref],
            cwd=dest,
        )
        if proc.returncode != 0:
            shutil.rmtree(dest, ignore_errors=True)
            raise HarnessGitError(
                "git_clone_failed",
                _redact((proc.stderr or "git fetch failed").strip()),
            )
        proc = _run(["git", "checkout", "-q", "FETCH_HEAD"], cwd=dest)
        if proc.returncode != 0:
            shutil.rmtree(dest, ignore_errors=True)
            raise HarnessGitError(
                "git_clone_failed",
                _redact((proc.stderr or "git checkout failed").strip()),
            )
        return
    # Symbolic ref path.
    proc = _run(
        ["git", "clone", "-q", "--depth=1", "--branch", ref, effective, dest],
    )
    if proc.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        stderr = proc.stderr or ""
        if "Authentication" in stderr or "could not read" in stderr.lower():
            code = "git_auth_failed"
        elif "Remote branch" in stderr or "not found" in stderr.lower():
            code = "git_ref_not_found"
        else:
            code = "git_clone_failed"
        raise HarnessGitError(code, _redact(stderr.strip() or "git clone failed"))


__all__ = ["HarnessGitError", "clone_at_ref", "ls_remote"]
