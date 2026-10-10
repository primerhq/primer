"""Git client wrapper for harness fetch/clone operations.

We shell out to `git` because pure-Python git libs add a heavy dep and
shelling is universal. Token redaction is done before any error surface.

Every url and ref is checked before git starts (:func:`validate_git_url`, :func:`validate_git_ref`), sits after a ``--`` in the
argv so git can never read it as an option, and every git runs with ``-c protocol.allow=never`` plus an explicit allow for
https (and file, under the operator opt-in) and ``GIT_PROTOCOL_FROM_USER=0``, so no other transport (``ext::``, ``ssh``,
``git://``, a local path) is reachable even through a value that slipped past the checks (security review 2026-10-08,
AUTHZ-04 / INJ-03 / FS-02).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlparse, urlunparse

import yaml

from primer.harness.hashes import hash_bundle
from primer.model.harness import file_git_urls_allowed, validate_git_ref, validate_git_url


class HarnessGitError(Exception):
    """Raised for any git-related failure with a stable error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


_GIT_TIMEOUT_SECONDS: Final = 300.0


def _check_target(url: str, ref: str) -> None:
    """Refuse a url or ref git must never see, before any process starts."""
    try:
        validate_git_url(url)
    except ValueError as exc:
        raise HarnessGitError("invalid_git_url", str(exc)) from exc
    try:
        validate_git_ref(ref)
    except ValueError as exc:
        raise HarnessGitError("invalid_git_ref", str(exc)) from exc


def _git(*args: str) -> list[str]:
    """The argv for one git call: only the https transport (plus file under the operator opt-in) is allowed."""
    protocols = ["-c", "protocol.allow=never", "-c", "protocol.https.allow=always"]
    if file_git_urls_allowed():
        protocols += ["-c", "protocol.file.allow=always"]
    return ["git", *protocols, *args]


def _git_env() -> dict[str, str]:
    """The environment for every git call: ``GIT_PROTOCOL_FROM_USER=0`` so a ``user``-policy transport is never allowed."""
    return {**os.environ, "GIT_PROTOCOL_FROM_USER": "0"}


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

# The userinfo of any OTHER url in the text: a credential kept IN the stored git_url (``https://ghp_xxx@github.com/org/repo``, the personal-access-token shape the harness api accepts and serves
# masked) is not ours to inject and not the stored ``git_token``, and git echoes the url it was given in stderr ("unable to access 'https://...'"). The userinfo runs to the last ``@`` before the
# first ``/``, ``?`` or ``#``. The scheme is ``[a-z][a-z0-9]*`` on purpose (the quadratic-scan note on ``primer.common.log._USERINFO``). The marker of our own prefix is left as it is.
_URL_USERINFO = re.compile(r'(\b[a-z][a-z0-9]*://)(?!oauth2:\*\*\*@)[^/?#\s"]*@', re.IGNORECASE)


def _redact(text: str, token: str | None = None) -> str:
    """Strip our injected ``oauth2:<token>@`` prefix; if ``token`` is known,
    also strip its bare appearance anywhere in the string (defence against
    git versions or credential-helpers that echo the secret elsewhere).
    The userinfo of any other url in the text (a credential kept in the stored
    ``git_url``) is masked too (ticket 01a11d32).
    """
    out = _TOKEN_PATTERN.sub("oauth2:***@", text)
    if token:
        # Replace the literal token; do not regex-escape with re.escape
        # since `out` is plain text not a pattern.
        out = out.replace(token, "***")
    if "@" in out and "://" in out:
        out = _URL_USERINFO.sub(r"\1[REDACTED]@", out)
    return out


async def _run(args: list[str], **kwargs) -> tuple[int, str, str]:
    """Run a git command asynchronously.

    Returns (returncode, stdout, stderr).  Raises HarnessGitError on
    timeout or if the git binary is not found.
    """
    cwd = kwargs.pop("cwd", None)
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=_git_env(),
            **kwargs,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(),
                timeout=_GIT_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            proc.kill()
            await proc.communicate()
            raise HarnessGitError("subprocess_error", "git command timed out") from exc
        return proc.returncode, stdout_b.decode("utf-8", errors="replace"), stderr_b.decode("utf-8", errors="replace")
    except HarnessGitError:
        raise
    except FileNotFoundError as exc:
        raise HarnessGitError(
            "subprocess_error", "git binary not found on PATH",
        ) from exc


async def ls_remote(url: str, *, token: str | None, ref: str) -> str:
    """Return the commit SHA pointed to by ``ref`` on the remote.

    Accepts branches, tags, and full SHAs (the SHA case skips network
    and returns the input).
    """
    _check_target(url, ref)
    if len(ref) == 40 and all(c in "0123456789abcdef" for c in ref):
        # full SHA — no need to ls-remote
        return ref
    effective = _inject_token(url, token)
    returncode, stdout, stderr = await _run(_git("ls-remote", "--", effective, ref))
    if returncode != 0:
        raise HarnessGitError(
            "git_clone_failed" if "Authentication" in (stderr or "") else "ref_not_found",
            _redact((stderr or "").strip() or "ls-remote failed", token),
        )
    lines = [ln for ln in (stdout or "").splitlines() if ln.strip()]
    if not lines:
        raise HarnessGitError(
            "ref_not_found", f"ref {ref!r} not found on remote",
        )
    sha = lines[0].split("\t", 1)[0].strip()
    if len(sha) != 40:
        raise HarnessGitError("ref_not_found", "could not parse ls-remote output")
    return sha


async def clone_at_ref(
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
    _check_target(url, ref)
    effective = _inject_token(url, token)
    is_sha = len(ref) == 40 and all(c in "0123456789abcdef" for c in ref)
    if is_sha:
        # SHA path: init empty, fetch the specific SHA, checkout.
        returncode, _, stderr = await _run(_git("init", "-q", "--", dest))
        if returncode != 0:
            raise HarnessGitError(
                "git_clone_failed",
                _redact((stderr or "git init failed").strip(), token),
            )
        returncode, _, stderr = await _run(
            _git("fetch", "--depth=1", "--", effective, ref),
            cwd=dest,
        )
        if returncode != 0:
            shutil.rmtree(dest, ignore_errors=True)
            raise HarnessGitError(
                "git_clone_failed",
                _redact((stderr or "git fetch failed").strip(), token),
            )
        returncode, _, stderr = await _run(_git("checkout", "-q", "FETCH_HEAD"), cwd=dest)
        if returncode != 0:
            shutil.rmtree(dest, ignore_errors=True)
            raise HarnessGitError(
                "git_clone_failed",
                _redact((stderr or "git checkout failed").strip(), token),
            )
        return
    # Symbolic ref path.
    returncode, _, stderr = await _run(
        _git("clone", "-q", "--depth=1", f"--branch={ref}", "--", effective, dest),
    )
    if returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        if "Authentication" in stderr or "could not read" in stderr.lower():
            code = "git_auth_failed"
        elif "Remote branch" in stderr or "not found" in stderr.lower():
            code = "git_ref_not_found"
        else:
            code = "git_clone_failed"
        raise HarnessGitError(code, _redact(stderr.strip() or "git clone failed", token))


async def fetch_harness_metadata(
    *,
    git_url: str,
    ref: str,
    subpath: str | None,
    token: str | None,
) -> tuple[dict[str, Any], dict[str, Any], str, str]:
    """Sparse-fetch harness.yaml + overrides.schema.json + bundle hash + SHA.

    Clones ``git_url`` at ``ref`` into a temp dir, reads the metadata
    files under ``subpath`` (or the repo root), computes a bundle hash
    over all non-.git files in the subpath subtree, resolves the commit
    SHA, then discards the clone. Returns
    ``(harness_yaml_dict, overrides_schema_dict, bundle_hash, resolved_commit)``.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        try:
            await clone_at_ref(git_url, token=token, ref=ref, dest=tmp_dir)
        except HarnessGitError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            raise HarnessGitError(
                "git_clone_failed", _redact(str(exc), token),
            ) from exc

        root = Path(tmp_dir)
        target = root / subpath if subpath else root

        harness_path = target / "harness.yaml"
        if not harness_path.is_file():
            raise HarnessGitError(
                "dependency_yaml_invalid",
                "harness.yaml missing or invalid",
            )
        try:
            harness_yaml = yaml.safe_load(harness_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise HarnessGitError(
                "dependency_yaml_invalid",
                _redact("harness.yaml missing or invalid", token),
            ) from exc
        if not isinstance(harness_yaml, dict):
            raise HarnessGitError(
                "dependency_yaml_invalid",
                "harness.yaml missing or invalid",
            )

        schema_path = target / "overrides.schema.json"
        if schema_path.is_file():
            try:
                overrides_schema = json.loads(schema_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise HarnessGitError(
                    "dependency_yaml_invalid",
                    "overrides.schema.json invalid",
                ) from exc
        else:
            overrides_schema = {"type": "object", "properties": {}}

        # Bundle hash: every non-.git file under target, path relative to target.
        files: list[tuple[str, bytes]] = []
        for p in sorted(target.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(target)
            parts = rel.parts
            if parts and parts[0] == ".git":
                continue
            files.append((rel.as_posix(), p.read_bytes()))
        bundle_hash = hash_bundle(files)

        # Resolve commit SHA from the working clone.
        try:
            proc = await asyncio.create_subprocess_exec(
                *_git("-C", tmp_dir, "rev-parse", "HEAD"),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_git_env(),
            )
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=_GIT_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            raise HarnessGitError(
                "subprocess_error", "git rev-parse timed out",
            ) from exc
        except FileNotFoundError as exc:
            raise HarnessGitError(
                "subprocess_error", "git binary not found on PATH",
            ) from exc
        if proc.returncode != 0:
            raise HarnessGitError(
                "git_clone_failed",
                _redact(
                    (stderr_b.decode("utf-8", errors="replace") or "git rev-parse failed").strip(),
                    token,
                ),
            )
        resolved_commit = stdout_b.decode("utf-8", errors="replace").strip()
        if len(resolved_commit) != 40:
            raise HarnessGitError(
                "git_clone_failed", "could not resolve commit SHA",
            )

        return harness_yaml, overrides_schema, bundle_hash, resolved_commit


async def _get_head_sha(clone_dir: str) -> str:
    """Return ``git rev-parse HEAD`` for ``clone_dir`` or empty string on failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *_git("-C", clone_dir, "rev-parse", "HEAD"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_git_env(),
        )
        stdout_b, _ = await asyncio.wait_for(
            proc.communicate(), timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (TimeoutError, FileNotFoundError):
        return ""
    if proc.returncode != 0:
        return ""
    sha = stdout_b.decode("utf-8", errors="replace").strip()
    if len(sha) != 40:
        return ""
    return sha


async def _run_checked(args: list[str], *, token: str | None = None, cwd: str | None = None,
                       error_code: str = "git_push_failed") -> tuple[str, str]:
    """Run a git command and raise HarnessGitError on non-zero exit.

    Returns (stdout, stderr) as strings on success. Token is redacted from
    any raised error message.
    """
    returncode, stdout, stderr = await _run(args, cwd=cwd)
    if returncode != 0:
        msg = (stderr or stdout or "git command failed").strip()
        raise HarnessGitError(error_code, _redact(msg, token))
    return stdout, stderr


async def push_bundle(
    *,
    url: str,
    token: str | None,
    ref: str,
    files: list[tuple[str, bytes]],
    subpath: str | None,
    commit_message: str,
    expected_remote_sha: str | None,
) -> str:
    """Clone ``url`` at ``ref``, write ``files`` under ``subpath``, commit, push.

    Returns the new commit SHA. Raises
    ``HarnessGitError(code='push_remote_diverged')`` when ``expected_remote_sha``
    is set and the remote has moved, or when the remote rejects the push as
    non-fast-forward. Returns the current HEAD SHA without a new commit when
    the working tree is unchanged (no-op).
    """
    _check_target(url, ref)
    auth_url = _inject_token(url, token)
    with tempfile.TemporaryDirectory() as td:
        clone_dir = os.path.join(td, "repo")
        # Try a shallow clone first; fall back to init+remote when the
        # remote is empty or the ref doesn't exist yet.
        try:
            await _run_checked(
                _git("clone", "--depth=1", f"--branch={ref}", "--", auth_url, clone_dir),
                token=token, error_code="git_clone_failed",
            )
        except HarnessGitError:
            shutil.rmtree(clone_dir, ignore_errors=True)
            await _run_checked(
                _git("init", f"--initial-branch={ref}", "--", clone_dir),
                token=token, error_code="git_clone_failed",
            )
            await _run_checked(
                _git("-C", clone_dir, "remote", "add", "--", "origin", auth_url),
                token=token, error_code="git_clone_failed",
            )

        # Remote-divergence check: if the caller said the remote SHOULD be at
        # ``expected_remote_sha``, confirm it before we touch anything.
        if expected_remote_sha is not None:
            try:
                actual = await ls_remote(url=url, token=token, ref=ref)
            except HarnessGitError as exc:
                # We expected the ref to exist; if ls-remote can't find it
                # the remote diverged (or someone deleted the ref).
                raise HarnessGitError(
                    "push_remote_diverged",
                    f"remote {ref} unavailable (expected {expected_remote_sha}): {exc.message}",
                ) from exc
            if actual != expected_remote_sha:
                raise HarnessGitError(
                    "push_remote_diverged",
                    f"remote {ref} moved (expected {expected_remote_sha}, found {actual})",
                )

        # Write files into the subpath subtree.
        base = os.path.join(clone_dir, subpath) if subpath else clone_dir
        os.makedirs(base, exist_ok=True)
        for entry in os.listdir(base):
            if entry == ".git":
                continue
            p = os.path.join(base, entry)
            if os.path.isdir(p):
                shutil.rmtree(p)
            else:
                os.remove(p)
        for rel, data in files:
            target = os.path.join(base, rel)
            parent = os.path.dirname(target) or base
            os.makedirs(parent, exist_ok=True)
            with open(target, "wb") as fh:
                fh.write(data)

        # Stage all changes.
        await _run_checked(
            _git("-C", clone_dir,
                 "-c", "user.email=primer@primer",
                 "-c", "user.name=primer",
                 "add", "-A"),
            token=token,
        )

        # No-op detection: if `git status --porcelain` is empty, skip commit.
        returncode, status_out, status_err = await _run(
            _git("-C", clone_dir, "status", "--porcelain"),
        )
        if returncode != 0:
            raise HarnessGitError(
                "git_push_failed",
                _redact((status_err or "git status failed").strip(), token),
            )
        if not status_out.strip():
            return await _get_head_sha(clone_dir)

        await _run_checked(
            _git("-C", clone_dir,
                 "-c", "user.email=primer@primer",
                 "-c", "user.name=primer",
                 "commit", "-m", commit_message),
            token=token,
        )

        try:
            await _run_checked(
                _git("-C", clone_dir, "push", "--", "origin", ref),
                token=token,
            )
        except HarnessGitError as exc:
            if "non-fast-forward" in exc.message or "non-fast-forward" in str(exc):
                raise HarnessGitError(
                    "push_remote_diverged",
                    "remote rejected non-fast-forward push",
                ) from exc
            # Already redacted by _run_checked.
            raise

        return await _get_head_sha(clone_dir)


__all__ = [
    "HarnessGitError",
    "clone_at_ref",
    "fetch_harness_metadata",
    "ls_remote",
    "push_bundle",
]
