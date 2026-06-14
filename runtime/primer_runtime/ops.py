"""File-operation handlers for the workspace runtime.

Each handler is an async function with signature::

    async def <op>(args: dict, workspace_root: str) -> dict

On success  → return a dict that becomes ``response["result"]``.
On expected errors → raise :class:`OpError` with an :class:`~protocol.ErrorCode`.
"""

from __future__ import annotations

import asyncio
import base64
import errno
import os
import pathlib
import stat as _stat_mod

from primer_runtime.protocol import ErrorCode


def _subprocess_timeout() -> float:
    """Read the global subprocess deadline from ``PRIMER_SUBPROCESS_TIMEOUT_SECONDS``.

    Defaults to 120 seconds, matching the API-side ``AppConfig`` default.
    The API server injects this env var into workspace pods/containers via
    the same ``PRIMER_*`` env-var convention used for other runtime knobs.
    """
    raw = os.environ.get("PRIMER_SUBPROCESS_TIMEOUT_SECONDS", "")
    try:
        return float(raw) if raw else 120.0
    except ValueError:
        return 120.0


# ---------------------------------------------------------------------------
# Git-state helpers (ported from primer.workspace.local.state -- do NOT
# import primer.*; the runtime package is self-contained)
# ---------------------------------------------------------------------------

# Separator characters used in the git log format string.
_RECORD_SEP = "\x1e"
_FIELD_SEP = "\x1f"

# Git log format: sha <FS> subject <FS> committer-date-iso <FS> trailers <RS>
# The %(trailers:only,unfold) placeholder expands each Key: Value trailer.
_GIT_LOG_FORMAT = f"%H{_FIELD_SEP}%s{_FIELD_SEP}%cI{_FIELD_SEP}%(trailers:only,unfold){_RECORD_SEP}"


def _parse_trailers(block: str) -> dict[str, str]:
    """Parse ``Key: value`` lines from a trailer block (ported from LocalStateRepo)."""
    result: dict[str, str] = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        result[key.strip()] = value.strip()
    return result


def _parse_log_records(stdout: str) -> list[dict]:
    """Parse git log output into a list of record dicts (ported from LocalStateRepo).

    Each dict has:
      sha, subject, committed_at (ISO-8601 str),
      workspace_id, session_id, agent_id, op, tool, call_id (str | None)
    """
    out: list[dict] = []
    for record in stdout.split(_RECORD_SEP):
        record = record.strip("\n")
        if not record:
            continue
        parts = record.split(_FIELD_SEP)
        if len(parts) < 4:
            continue
        sha, subject, committed_at_iso, trailer_block = (
            parts[0],
            parts[1],
            parts[2],
            parts[3],
        )
        trailers = _parse_trailers(trailer_block)
        out.append(
            {
                "sha": sha,
                "subject": subject,
                "committed_at": committed_at_iso,
                "workspace_id": trailers.get("X-Primer-Workspace"),
                "session_id": trailers.get("X-Primer-Session"),
                "agent_id": trailers.get("X-Primer-Agent"),
                "op": trailers.get("X-Primer-Op"),
                "tool": trailers.get("X-Primer-Tool"),
                "call_id": trailers.get("X-Primer-Call"),
            }
        )
    return out


async def _run_git(state_dir: str, *args: str, allow_empty_repo: bool = False) -> tuple[str, str]:
    """Run ``git -C <state_dir> <args...>``, return (stdout, stderr) as text.

    Raises :class:`OpError` (EINTERNAL) on non-zero exit unless ``allow_empty_repo``
    is True and git exits 128 complaining about an empty repo.

    Kills the subprocess and raises :class:`OpError` (EINTERNAL) if the
    process exceeds the deadline read from ``PRIMER_SUBPROCESS_TIMEOUT_SECONDS``
    (default 120 s).
    """
    timeout = _subprocess_timeout()
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        state_dir,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        raise OpError(
            ErrorCode.EINTERNAL,
            f"git {' '.join(args)} timed out after {timeout}s",
        )
    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        if (
            allow_empty_repo
            and proc.returncode == 128
            and "does not have any commits yet" in stderr
        ):
            return "", stderr
        raise OpError(
            ErrorCode.EINTERNAL,
            f"git {' '.join(args)} exited {proc.returncode}: {stderr.strip()}",
        )
    return stdout, stderr


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------


class OpError(Exception):
    """Raised by op handlers to signal a well-known error condition."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def _resolve_safe(raw_path: str, workspace_root: str) -> pathlib.Path:
    """Resolve *raw_path* relative to *workspace_root* and verify it doesn't escape.

    Raises :class:`OpError` with ``EACCES`` if the resolved path is outside
    the workspace root.
    """
    root = pathlib.Path(workspace_root).resolve()
    # Absolute paths are taken as-is; relative paths are anchored to root.
    if pathlib.PurePosixPath(raw_path).is_absolute():
        candidate = pathlib.Path(raw_path).resolve()
    else:
        candidate = (root / raw_path).resolve()

    # Verify the resolved path is inside (or equal to) workspace root.
    try:
        candidate.relative_to(root)
    except ValueError:
        raise OpError(ErrorCode.EACCES, f"Path escapes workspace root: {raw_path!r}")

    return candidate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _os_err_to_op_error(exc: OSError, path: str) -> OpError:
    """Map an :class:`OSError` to the appropriate :class:`OpError`."""
    if exc.errno == errno.ENOENT:
        return OpError(ErrorCode.ENOENT, f"No such file or directory: {path!r}")
    if exc.errno == errno.EISDIR:
        return OpError(ErrorCode.EISDIR, f"Is a directory: {path!r}")
    if exc.errno in (errno.EACCES, errno.EPERM):
        return OpError(ErrorCode.EACCES, f"Permission denied: {path!r}")
    if exc.errno == errno.ENOTDIR:
        return OpError(ErrorCode.ENOTDIR, f"Not a directory: {path!r}")
    if exc.errno == errno.EEXIST:
        return OpError(ErrorCode.EEXIST, f"Already exists: {path!r}")
    return OpError(ErrorCode.EINTERNAL, f"OS error for {path!r}: {exc}")


def _stat_to_dict(st: os.stat_result, full_path: str, name: str) -> dict:
    """Convert an :func:`os.stat_result` to the wire ``FileStat`` dict."""
    return {
        "name": name,
        "path": full_path,
        "size": st.st_size,
        "mtime": st.st_mtime,
        "mode": st.st_mode,
        "is_dir": _stat_mod.S_ISDIR(st.st_mode),
    }


# ---------------------------------------------------------------------------
# Op handlers
# ---------------------------------------------------------------------------


async def read_file(args: dict, workspace_root: str) -> dict:
    """``read_file`` op → ``{content_b64}``."""
    raw_path: str = args.get("path", "")
    resolved = _resolve_safe(raw_path, workspace_root)

    def _read() -> bytes:
        try:
            st = os.stat(resolved)
        except OSError as exc:
            raise _os_err_to_op_error(exc, raw_path)
        if _stat_mod.S_ISDIR(st.st_mode):
            raise OpError(ErrorCode.EISDIR, f"Is a directory: {raw_path!r}")
        try:
            with open(resolved, "rb") as fh:
                return fh.read()
        except OSError as exc:
            raise _os_err_to_op_error(exc, raw_path)

    content = await asyncio.to_thread(_read)
    return {"content_b64": base64.b64encode(content).decode()}


async def write_file(args: dict, workspace_root: str) -> dict:
    """``write_file`` op → ``{ok}``; creates parent directories."""
    raw_path: str = args.get("path", "")
    content_b64: str = args.get("content_b64", "")
    mode: int | None = args.get("mode")

    resolved = _resolve_safe(raw_path, workspace_root)

    try:
        content = base64.b64decode(content_b64)
    except Exception as exc:
        raise OpError(ErrorCode.EPROTOCOL, f"Invalid base64 for content_b64: {exc}")

    def _write() -> None:
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise _os_err_to_op_error(exc, raw_path)
        try:
            with open(resolved, "wb") as fh:
                fh.write(content)
            if mode is not None:
                os.chmod(resolved, mode)
        except OSError as exc:
            raise _os_err_to_op_error(exc, raw_path)

    await asyncio.to_thread(_write)
    return {"ok": True}


async def append_line(args: dict, workspace_root: str) -> dict:
    """``append_line`` op → ``{ok, byte_offset}``; atomic O_APPEND single-line append."""
    raw_path: str = args.get("path", "")
    line_b64: str = args.get("line_b64", "")

    resolved = _resolve_safe(raw_path, workspace_root)

    try:
        line = base64.b64decode(line_b64)
    except Exception as exc:
        raise OpError(ErrorCode.EPROTOCOL, f"Invalid base64 for line_b64: {exc}")

    # Ensure line ends with a newline
    if not line.endswith(b"\n"):
        line = line + b"\n"

    def _append() -> int:
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise _os_err_to_op_error(exc, raw_path)
        try:
            # O_APPEND ensures atomic single-write at POSIX level on local FS.
            fd = os.open(str(resolved), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o666)
            try:
                os.write(fd, line)
                byte_offset = os.lseek(fd, 0, os.SEEK_CUR)
            finally:
                os.close(fd)
            return byte_offset
        except OSError as exc:
            raise _os_err_to_op_error(exc, raw_path)

    byte_offset = await asyncio.to_thread(_append)
    return {"ok": True, "byte_offset": byte_offset}


async def list_dir(args: dict, workspace_root: str) -> dict:
    """``list_dir`` op → ``{entries: [FileStat]}``."""
    raw_path: str = args.get("path", "")
    resolved = _resolve_safe(raw_path, workspace_root)

    def _list() -> list[dict]:
        try:
            entries_raw = list(os.scandir(resolved))
        except NotADirectoryError as exc:
            raise OpError(ErrorCode.ENOTDIR, f"Not a directory: {raw_path!r}")
        except FileNotFoundError:
            raise OpError(ErrorCode.ENOENT, f"No such file or directory: {raw_path!r}")
        except OSError as exc:
            raise _os_err_to_op_error(exc, raw_path)

        result = []
        for entry in entries_raw:
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue  # skip entries we cannot stat
            result.append(_stat_to_dict(st, entry.path, entry.name))
        return result

    entries = await asyncio.to_thread(_list)
    return {"entries": entries}


async def stat(args: dict, workspace_root: str) -> dict:
    """``stat`` op → ``{stat: FileStat | null}`` (null if path doesn't exist)."""
    raw_path: str = args.get("path", "")
    resolved = _resolve_safe(raw_path, workspace_root)

    def _stat() -> dict | None:
        try:
            st = os.stat(resolved)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise _os_err_to_op_error(exc, raw_path)
        name = resolved.name
        return _stat_to_dict(st, str(resolved), name)

    file_stat = await asyncio.to_thread(_stat)
    return {"stat": file_stat}


async def delete(args: dict, workspace_root: str) -> dict:
    """``delete`` op → ``{ok}`` (file or empty directory)."""
    raw_path: str = args.get("path", "")
    resolved = _resolve_safe(raw_path, workspace_root)

    def _delete() -> None:
        try:
            st = os.stat(resolved)
        except OSError as exc:
            raise _os_err_to_op_error(exc, raw_path)
        if _stat_mod.S_ISDIR(st.st_mode):
            try:
                os.rmdir(resolved)
            except OSError as exc:
                raise _os_err_to_op_error(exc, raw_path)
        else:
            try:
                os.unlink(resolved)
            except OSError as exc:
                raise _os_err_to_op_error(exc, raw_path)

    await asyncio.to_thread(_delete)
    return {"ok": True}


# ---------------------------------------------------------------------------
# State-repo op handlers  (in-pod git operations against <workspace_root>/.state)
# ---------------------------------------------------------------------------


async def _ensure_state_repo(state_dir: str) -> None:
    """Ensure ``<state_dir>/.git`` exists, initialising if needed.

    Idempotent: if the repo is already initialised this is a fast no-op
    (stat only).  Called at the start of every mutating state op so that
    callers do not need a separate initialisation step.

    All git subprocesses are bounded by the deadline from
    ``PRIMER_SUBPROCESS_TIMEOUT_SECONDS``; a hung ``git init`` or ``git
    config`` raises :class:`OpError` (EINTERNAL) and kills the process.
    """
    git_dir = os.path.join(state_dir, ".git")
    if os.path.isdir(git_dir):
        return
    timeout = _subprocess_timeout()
    os.makedirs(state_dir, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", state_dir, "init", "--initial-branch=main",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        raise OpError(
            ErrorCode.EINTERNAL,
            f"git init timed out after {timeout}s",
        )
    if proc.returncode != 0:
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        raise OpError(
            ErrorCode.EINTERNAL,
            f"git init failed (rc={proc.returncode}): {stderr.strip()}",
        )
    # Configure identity so commits never fail due to missing global git config.
    for cfg_args in (
        ["config", "user.email", "primer@local"],
        ["config", "user.name", "primer"],
    ):
        proc2 = await asyncio.create_subprocess_exec(
            "git", "-C", state_dir, *cfg_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(proc2.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc2.kill()
            except ProcessLookupError:
                pass
            await proc2.wait()
            raise OpError(
                ErrorCode.EINTERNAL,
                f"git config timed out after {timeout}s",
            )


async def state_commit(args: dict, workspace_root: str) -> dict:
    """``state_commit`` op: write files, git-rm deletes, commit, return sha.

    args:
      files      dict[str, str]  -- {relative-path: content_b64}
      deletes    list[str]       -- relative paths to git rm (optional)
      message    str             -- full commit message (may include trailers)
      allow_empty bool           -- passed as --allow-empty flag (optional)

    Returns: {"sha": "<40-hex>"}
    """
    files: dict[str, str] = args.get("files") or {}
    deletes: list[str] = args.get("deletes") or []
    message: str = args.get("message", "")
    allow_empty: bool = bool(args.get("allow_empty", False))

    state_dir = os.path.join(workspace_root, ".state")
    await _ensure_state_repo(state_dir)

    # Write files to disk and collect paths to stage.
    staged: list[str] = []
    for rel_path, content_b64 in files.items():
        try:
            content = base64.b64decode(content_b64)
        except Exception as exc:
            raise OpError(ErrorCode.EPROTOCOL, f"Invalid base64 for {rel_path!r}: {exc}")
        dest = pathlib.Path(state_dir) / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        staged.append(rel_path)

    # Stage new/modified files.
    if staged:
        await _run_git(state_dir, "add", "--", *staged)

    # Delete files.
    if deletes:
        await _run_git(state_dir, "rm", "--quiet", "--ignore-unmatch", "--", *deletes)

    # Commit.
    commit_args = ["commit", "--quiet", "-m", message]
    if allow_empty:
        commit_args.insert(1, "--allow-empty")
    await _run_git(state_dir, *commit_args)

    # Return the HEAD sha.
    sha_raw, _ = await _run_git(state_dir, "rev-parse", "HEAD")
    return {"sha": sha_raw.strip()}


async def state_read(args: dict, workspace_root: str) -> dict:
    """``state_read`` op: read files from <workspace_root>/.state by path.

    args:
      paths  list[str]  -- relative paths to read

    Returns: {"files": {path: content_b64_or_null}}
    where each value is a base64 string if the file exists, or null if absent.
    """
    paths: list[str] = args.get("paths") or []
    state_dir = pathlib.Path(workspace_root) / ".state"

    result: dict[str, str | None] = {}
    for rel_path in paths:
        target = state_dir / rel_path
        if target.exists() and target.is_file():
            content = target.read_bytes()
            result[rel_path] = base64.b64encode(content).decode()
        else:
            result[rel_path] = None

    return {"files": result}


async def state_history(args: dict, workspace_root: str) -> dict:
    """``state_history`` op: return git log of <workspace_root>/.state.

    args:
      limit       int         -- max commits to return (default 100)
      session_id  str|None    -- filter by X-Primer-Session trailer
      agent_id    str|None    -- filter by X-Primer-Agent trailer

    Returns: {"commits": [<record-dict>, ...]}
    Each record has: sha, subject, committed_at (ISO-8601), workspace_id,
    session_id, agent_id, op, tool, call_id (all str|None except sha/subject/committed_at).
    Newest first.
    """
    limit: int = int(args.get("limit", 100))
    session_id: str | None = args.get("session_id")
    agent_id: str | None = args.get("agent_id")

    state_dir = os.path.join(workspace_root, ".state")

    git_args = [
        "log",
        f"--max-count={limit}",
        f"--format={_GIT_LOG_FORMAT}",
    ]
    if session_id is not None:
        git_args += ["--grep", f"^X-Primer-Session: {session_id}$"]
    if agent_id is not None:
        git_args += ["--grep", f"^X-Primer-Agent: {agent_id}$"]
    if session_id is not None and agent_id is not None:
        git_args.append("--all-match")

    stdout, _ = await _run_git(state_dir, *git_args, allow_empty_repo=True)
    commits = _parse_log_records(stdout)
    return {"commits": commits}


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

HANDLERS: dict[str, object] = {
    "read_file": read_file,
    "write_file": write_file,
    "append_line": append_line,
    "list_dir": list_dir,
    "stat": stat,
    "delete": delete,
    "state_commit": state_commit,
    "state_read": state_read,
    "state_history": state_history,
}
