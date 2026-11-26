"""File-operation handlers for the workspace runtime.

Each handler is an async function with signature::

    async def <op>(args: dict, workspace_root: str, locks: WorkspaceLockTable) -> dict

The ``locks`` arg is uniform so the server can dispatch every op the same
way; read-only ops (``read_file``/``list_dir``/``stat``) and the state ops
accept it with a ``None`` default and ignore it, while the mutating ops
(``write_file``/``append_line``/``delete``) acquire a Tier-A write lock.

On success  → return a dict that becomes ``response["result"]``.
On expected errors → raise :class:`OpError` with an :class:`~protocol.ErrorCode`.
"""

from __future__ import annotations

import asyncio
import base64
import errno
import fnmatch
import hashlib
import logging
import os
import pathlib
import stat as _stat_mod
import tempfile
import time as _time

from collections import deque
from collections.abc import Callable, Coroutine
from typing import Any

import regex

from primer_runtime.locks import WorkspaceLockTable
from primer_runtime.protocol import ErrorCode, Response, serialize

log = logging.getLogger(__name__)

# Emit a debug trace when a writer parks longer than this on a busy lock.
_LOCK_WAIT_TRACE_S: float = 0.25


def _strict_write_locking() -> bool:
    """Whether exec/tool scope locks collapse to the workspace root.

    Read from ``PRIMER_STRICT_WRITE_LOCKING`` (injected by the backend
    when the template opts in). Mirrors ``_subprocess_timeout`` (:23).
    """
    raw = os.environ.get("PRIMER_STRICT_WRITE_LOCKING", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _scope_key(resolved: pathlib.Path, workspace_root: str) -> str:
    """Tier-B/A scope key: the parent dir, or the workspace root if strict."""
    if _strict_write_locking():
        return str(pathlib.Path(workspace_root).resolve())
    return str(resolved.parent)


def _atomic_write_bytes(target: pathlib.Path, content: bytes, mode: int | None) -> None:
    """Temp-file + os.replace atomic write.

    Same pattern as the local backend's 2-arg helper at
    primer/workspace/local/workspace.py:736, plus a ``mode`` param (this
    runtime copy is a superset, not byte-identical)."""
    directory = target.parent
    existing_mode: int | None = None
    try:
        existing_mode = os.stat(target).st_mode
    except OSError:
        existing_mode = None
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(directory)
    )
    tmp_path = pathlib.Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp_path, mode)
        elif existing_mode is not None:
            os.chmod(tmp_path, existing_mode)
        else:
            # mkstemp makes 0600; a new file gets what a plain open would give it (review of workspaces, W55a).
            umask = os.umask(0)
            os.umask(umask)
            os.chmod(tmp_path, 0o666 & ~umask)
        os.replace(tmp_path, target)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


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

    Stops the subprocess (SIGTERM, then SIGKILL) and raises :class:`OpError`
    (EINTERNAL) if the process exceeds the deadline read from
    ``PRIMER_SUBPROCESS_TIMEOUT_SECONDS`` (default 120 s). A failure on an
    ``index.lock`` older than that deadline removes the lock and retries once.
    """
    try:
        return await _run_git_once(state_dir, args, allow_empty_repo)
    except OpError as exc:
        # A lock left by a git that was killed outright (an OOM kill, an older SIGKILL) is older than any live git holds it.
        if "index.lock" not in exc.message or not _remove_stale_index_lock(state_dir):
            raise
    return await _run_git_once(state_dir, args, allow_empty_repo)


def _remove_stale_index_lock(state_dir: str) -> bool:
    """Remove ``.git/index.lock`` when it is older than the subprocess timeout; True when removed (or already gone)."""
    lock = pathlib.Path(state_dir) / ".git" / "index.lock"
    try:
        age = _time.time() - lock.stat().st_mtime
        if age <= _subprocess_timeout():
            return False
        lock.unlink()
    except FileNotFoundError:
        return True  # gone since the failure: the retry may proceed
    except OSError:
        return False
    log.warning("removed a stale %s (%.0fs old)", lock, age)
    return True


# How long a git that timed out gets to clean up after SIGTERM before SIGKILL.
_TERMINATE_GRACE_S: float = 2.0


async def _stop_git(proc: asyncio.subprocess.Process) -> None:
    """SIGTERM first: git removes its lockfiles on it. SIGKILL left ``.git/index.lock`` behind and wedged every later
    commit (GI-6)."""
    try:
        proc.terminate()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=_TERMINATE_GRACE_S)
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()


async def _run_git_once(state_dir: str, args: tuple[str, ...], allow_empty_repo: bool) -> tuple[str, str]:
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
    except TimeoutError:
        await _stop_git(proc)
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


def _resolve_entry_safe(raw_path: str, workspace_root: str) -> pathlib.Path:
    """The entry ``raw_path`` names, for an operation on the entry itself (delete): :func:`_resolve_safe`, except that when the final component is a
    symlink the entry is the LINK (review of workspaces, W15). The directory it lives in is resolved and contained; the link's target is neither
    followed nor judged, so a link to a file outside the workspace is removed as a link and the outside file is untouched."""
    root = pathlib.Path(workspace_root).resolve()
    lexical_raw = pathlib.PurePosixPath(raw_path) if pathlib.PurePosixPath(raw_path).is_absolute() else None
    lexical = pathlib.Path(os.path.normpath(raw_path if lexical_raw is not None else root / raw_path))
    if lexical.name in ("", ".", "..") or lexical == root:
        return _resolve_safe(raw_path, workspace_root)
    entry = lexical.parent.resolve() / lexical.name
    if not entry.is_symlink():
        return _resolve_safe(raw_path, workspace_root)
    try:
        entry.parent.relative_to(root)
    except ValueError:
        raise OpError(ErrorCode.EACCES, f"Path escapes workspace root: {raw_path!r}")
    return entry


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


def _refuse_unless_regular(mode: int, raw_path: str) -> None:
    """Raise the op error for a ``read_file`` of anything but a regular file: EISDIR for a directory, EUNSUPPORTED for the rest (a FIFO,
    a socket, a character or block device), whose read could block or never end."""
    if _stat_mod.S_ISDIR(mode):
        raise OpError(ErrorCode.EISDIR, f"Is a directory: {raw_path!r}")
    if not _stat_mod.S_ISREG(mode):
        raise OpError(
            ErrorCode.EUNSUPPORTED,
            f"Not a regular file (a FIFO, socket or device is not read): {raw_path!r}",
        )


async def read_file(
    args: dict, workspace_root: str, locks: WorkspaceLockTable | None = None
) -> dict:
    """``read_file`` op → ``{content_b64}``; read-only, takes no lock. ``offset`` (protocol 1.5, optional) starts the read at that byte,
    and ``limit`` (protocol 1.8, optional) caps how many bytes are returned (offset + limit, so the platform can page any window of a
    big file in bounded-size frames; a whole-file read would land in one frame and can drop the connection for very large files,
    review 2026-11-23, 5.12).

    Only a regular file is read. The ``stat`` of the path is checked first, and then the file is opened with ``O_NONBLOCK`` and the
    DESCRIPTOR is checked with ``fstat``, so the check and the read are about the same file: a process in the workspace that swaps a
    FIFO in after the ``stat`` would otherwise pin this thread in ``open()`` (a FIFO with no writer blocks there) or in ``read()``, and
    the platform's 5 s timeout only abandons such a thread, so enough of them exhaust the pool (the G5 audit, G5-09). ``O_NONBLOCK``
    changes nothing for a regular file. ``O_NOFOLLOW`` refuses a symbolic link swapped in after the path was resolved (``EACCES``).
    """
    raw_path: str = args.get("path", "")
    resolved = _resolve_safe(raw_path, workspace_root)
    # Protocol 1.5: read from this byte offset (a tap reads only what a log gained). Absent or null: from the start, as before.
    offset = args.get("offset")
    if offset is not None and (isinstance(offset, bool) or not isinstance(offset, int) or offset < 0):
        raise OpError(ErrorCode.EPROTOCOL, f"offset must be a non-negative integer, got {offset!r}")
    # Protocol 1.8: return at most this many bytes. Absent or null: as many as the file has (the legacy whole-file read).
    limit = args.get("limit")
    if limit is not None and (
        isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0
    ):
        raise OpError(ErrorCode.EPROTOCOL, f"limit must be a positive integer, got {limit!r}")

    def _read() -> bytes:
        try:
            st = os.stat(resolved)
        except OSError as exc:
            raise _os_err_to_op_error(exc, raw_path)
        _refuse_unless_regular(st.st_mode, raw_path)
        try:
            # O_NOFOLLOW: the path is already resolved, so a symbolic link here was swapped in after the containment check, and
            # following it could leave the workspace root (the review of #869, F7).
            fd = os.open(resolved, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise OpError(ErrorCode.EACCES, f"A symbolic link was swapped in and is not followed: {raw_path!r}")
            raise _os_err_to_op_error(exc, raw_path)
        try:
            _refuse_unless_regular(os.fstat(fd).st_mode, raw_path)
            if offset:
                os.lseek(fd, offset, os.SEEK_SET)  # past the end reads nothing
            chunks: list[bytes] = []
            remaining = limit
            while remaining is None or remaining > 0:
                n = 1 << 20 if remaining is None else min(1 << 20, remaining)
                chunk = os.read(fd, n)
                if not chunk:
                    break
                chunks.append(chunk)
                if remaining is not None:
                    remaining -= len(chunk)
            return b"".join(chunks)
        except OSError as exc:
            raise _os_err_to_op_error(exc, raw_path)
        finally:
            os.close(fd)

    content = await asyncio.to_thread(_read)
    return {"content_b64": base64.b64encode(content).decode()}


async def capture_service_bundle(args: dict, workspace_root: str, locks: WorkspaceLockTable | None = None) -> dict:
    from primer_runtime.directory_bundle import CaptureBusyError, capture_bounded

    path, reserved = args.get("path"), args.get("reserved")
    if not isinstance(path, str) or not isinstance(reserved, list) or not all(isinstance(p, str) for p in reserved):
        raise OpError(ErrorCode.EPROTOCOL, "expected a directory path and reserved workspace paths")
    try:
        data = await capture_bounded(workspace_root, path, tuple(reserved))
    except CaptureBusyError as exc:
        raise OpError(ErrorCode.EBUSY, str(exc)) from exc
    except TimeoutError as exc:
        raise OpError(ErrorCode.ETIMEDOUT, "workspace bundle capture timed out") from exc
    except (ValueError, OSError) as exc:
        raise OpError(ErrorCode.EACCES, f"could not capture bundle directory: {exc}") from exc
    return {"content_b64": base64.b64encode(data).decode("ascii")}


async def write_file(args: dict, workspace_root: str, locks: WorkspaceLockTable) -> dict:
    """``write_file`` op → ``{ok}``; atomic; Tier-A locked; creates parents."""
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
            _atomic_write_bytes(resolved, content, mode)
        except OSError as exc:
            raise _os_err_to_op_error(exc, raw_path)

    _t0 = _time.monotonic()
    async with locks.hold_write(_scope_key(resolved, workspace_root), str(resolved)):
        waited = _time.monotonic() - _t0
        if waited > _LOCK_WAIT_TRACE_S:
            log.debug("write_file waited %.3fs on busy lock for %r", waited, raw_path)
        await asyncio.to_thread(_write)
    return {"ok": True}


async def append_line(args: dict, workspace_root: str, locks: WorkspaceLockTable) -> dict:
    """``append_line`` op → ``{ok, byte_offset}``; atomic O_APPEND, Tier-A locked."""
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

    _t0 = _time.monotonic()
    async with locks.hold_write(_scope_key(resolved, workspace_root), str(resolved)):
        waited = _time.monotonic() - _t0
        if waited > _LOCK_WAIT_TRACE_S:
            log.debug("append_line waited %.3fs on busy lock for %r", waited, raw_path)
        byte_offset = await asyncio.to_thread(_append)
    return {"ok": True, "byte_offset": byte_offset}


async def list_dir(
    args: dict, workspace_root: str, locks: WorkspaceLockTable | None = None
) -> dict:
    """``list_dir`` op → ``{entries: [FileStat]}``; read-only, takes no lock."""
    raw_path: str = args.get("path", "")
    resolved = _resolve_safe(raw_path, workspace_root)

    def _list() -> list[dict]:
        try:
            entries_raw = list(os.scandir(resolved))
        except NotADirectoryError:
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


async def stat(
    args: dict, workspace_root: str, locks: WorkspaceLockTable | None = None
) -> dict:
    """``stat`` op → ``{stat: FileStat | null}``; read-only, takes no lock."""
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


async def delete(args: dict, workspace_root: str, locks: WorkspaceLockTable) -> dict:
    """``delete`` op → ``{ok}`` (file or empty directory); Tier-A locked."""
    raw_path: str = args.get("path", "")
    resolved = _resolve_entry_safe(raw_path, workspace_root)

    def _delete() -> None:
        try:
            st = os.lstat(resolved)  # a symlink is deleted as the link it is: its target is never touched (W15)
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

    _t0 = _time.monotonic()
    async with locks.hold_write(_scope_key(resolved, workspace_root), str(resolved)):
        waited = _time.monotonic() - _t0
        if waited > _LOCK_WAIT_TRACE_S:
            log.debug("delete waited %.3fs on busy lock for %r", waited, raw_path)
        await asyncio.to_thread(_delete)
    return {"ok": True}


# ---------------------------------------------------------------------------
# walk / search / write_file_cas ops (protocol 1.8)
# ---------------------------------------------------------------------------
#
# The sandbox ``glob`` and ``grep`` tools used to make one ``list_dir``
# round-trip per directory (and one ``read_file`` round-trip per file) with
# no caps, so a wide tree meant thousands of WebSocket frames in the API
# process (review 2026-11-23, 5.11). Both now run in the runtime in ONE call,
# under an entry/file cap and a wall-clock deadline.


_WALK_MAX_ENTRIES_DEFAULT: int = 25000
_OP_TIMEOUT_S_DEFAULT: float = 30.0
_SEARCH_MAX_FILES_DEFAULT: int = 2000
_SEARCH_MAX_FILE_BYTES_DEFAULT: int = 1 << 20
_SEARCH_MAX_MATCHES_DEFAULT: int = 5000
_BINARY_SNIFF_BYTES: int = 1024
_SEARCH_MAX_OUTPUT_BYTES_DEFAULT: int = 4 << 20
# One line's regex match may run this long before the search stops with ``regex_timeout``
# (well under the op deadline); matching releases the GIL, so the event loop keeps running.
_SEARCH_LINE_TIMEOUT_S: float = 2.0


def _float_arg(value: Any, name: str, default: float) -> float:
    """A positive float op argument; absent/null means *default*."""
    if value is None:
        return default
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise OpError(ErrorCode.EPROTOCOL, f"{name} must be a number, got {value!r}") from exc
    if result <= 0:
        raise OpError(ErrorCode.EPROTOCOL, f"{name} must be positive, got {value!r}")
    return result


def _int_arg(value: Any, name: str, default: int) -> int:
    """A positive integer op argument; absent/null means *default*."""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise OpError(ErrorCode.EPROTOCOL, f"{name} must be a positive integer, got {value!r}")
    return value


def _glob_filter_matches(rel: str, pattern: str) -> bool:
    """Whether a grep ``glob`` filter keeps the file at *rel* (ported from
    the platform's ``primer.workspace.local.tools.grep.glob_matches``; the
    runtime image has no ``primer`` package). A pattern without ``/`` matches
    the basename, one with ``/`` the relative path; fnmatch's ``*`` crosses
    ``/``, so ``**/`` must also match no directory (``**/*.py`` keeps
    ``a.py``)."""
    if "/" not in pattern:
        return fnmatch.fnmatch(rel.rsplit("/", 1)[-1], pattern)
    return fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(
        rel, pattern.replace("**/", "")
    )


def _walk_tree(
    root: pathlib.Path,
    max_entries: int,
    deadline: float,
    skip: tuple[str, ...] = (),
    files_only: bool = False,
    skip_prefix: str = "",
    file_filter: Callable[[str], bool] | None = None,
) -> tuple[list[dict], bool, bool, bool]:
    """One recursive walk of *root*, meant to run in a worker thread, breadth-first with each
    directory's children name-sorted (so shallow directories are reached before deep ones and a big
    early directory cannot use up the cap before its siblings are listed). Returns
    ``(entries, entry_cap_hit, deadline_hit, pending_at_cap)``: one dict per entry (``path``
    relative to *root*, ``is_dir``, ``size``, ``mtime``; directories always ``size`` 0), whether
    the entry cap stopped the walk, whether the deadline did, and whether entries were left
    UNVISITED when the cap stopped it (later children of the directory in hand, or directories
    still queued) - i.e. whether the cap actually dropped something. A walk that fills the cap
    on the tree's very last entry hit it but dropped nothing (exactly ``max_entries`` exist).
    Either of the first two flags stops the WHOLE walk, not just the directory in hand.

    Symlinks are reported (in the all-entries walk) but never FOLLOWED: a symlinked directory is a
    non-directory entry, as ``list_dir``'s ``stat(follow_symlinks=False)`` does. The files-only walk
    lists REGULAR files only (``lstat``): a symlink, FIFO, socket or device is skipped, because
    opening a FIFO would block a worker thread until something writes to it. A *skip* name (the
    reserved trees) is neither listed nor descended into; it is compared against the path relative
    to the workspace root (*skip_prefix* is *root*'s own workspace-relative path), not against
    *root*. *file_filter* (files-only walk) is applied to a file's path relative to *root* BEFORE it is
    counted against *max_entries*, so a cap fills with files the caller wants (a ``*.py`` search is not
    used up by ``node_modules`` data files that the glob would discard).
    """
    entries: list[dict] = []
    cap_hit = False
    deadline_hit = False
    pending_at_cap = False
    queue: deque[tuple[pathlib.Path, str]] = deque([(root, "")])
    while queue and not cap_hit and not deadline_hit:
        directory, rel_prefix = queue.popleft()
        if _time.monotonic() > deadline:
            deadline_hit = True
            break
        try:
            scandir_it = os.scandir(directory)
        except OSError:
            continue  # raced with a delete, or not a directory
        with scandir_it:
            children = list(scandir_it)
        children.sort(key=lambda e: e.name)
        for index, entry in enumerate(children):
            if _time.monotonic() > deadline:
                deadline_hit = True
                break
            rel = f"{rel_prefix}/{entry.name}" if rel_prefix else entry.name
            if skip:
                ws_rel = f"{skip_prefix}/{rel}" if skip_prefix else rel
                if any(ws_rel == s or ws_rel.startswith(s + "/") for s in skip):
                    continue
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                queue.append((pathlib.Path(entry.path), rel))
                if not files_only:
                    entries.append({"path": rel, "is_dir": True, "size": 0, "mtime": 0.0})
                    if len(entries) >= max_entries:
                        cap_hit = True
                        # Were entries left unvisited? (later children of this
                        # directory, or directories still queued)
                        pending_at_cap = index + 1 < len(children) or bool(queue)
                        break
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue  # raced with a delete
            if files_only and not _stat_mod.S_ISREG(st.st_mode):
                continue
            if files_only and file_filter is not None and not file_filter(rel):
                continue
            entries.append(
                {
                    "path": rel,
                    "is_dir": False,
                    "size": int(st.st_size),
                    "mtime": float(st.st_mtime),
                }
            )
            if len(entries) >= max_entries:
                cap_hit = True
                # Were entries left unvisited? (later children of this
                # directory, or directories still queued)
                pending_at_cap = index + 1 < len(children) or bool(queue)
                break
    return entries, cap_hit, deadline_hit, pending_at_cap


async def walk(
    args: dict, workspace_root: str, locks: WorkspaceLockTable | None = None
) -> dict:
    """``walk`` op (protocol 1.8) → ``{entries, truncated}``; read-only, takes no lock.

    One recursive directory walk in the runtime instead of one ``list_dir``
    round-trip per directory (review 2026-11-23, 5.11). Entries carry their
    path relative to the walked root plus kind, size and mtime, so the
    platform can match globs and sort newest-first with no further calls.
    Caps: at most ``max_entries`` entries (default 25000) and a ``timeout_s``
    deadline (default 30 s) checked between directories and between entries;
    hitting either stops the walk and sets ``truncated``.
    """
    raw_path: str = args.get("path", "")
    resolved = _resolve_safe(raw_path, workspace_root)
    max_entries = _int_arg(args.get("max_entries"), "max_entries", _WALK_MAX_ENTRIES_DEFAULT)
    timeout_s = _float_arg(args.get("timeout_s"), "timeout_s", _OP_TIMEOUT_S_DEFAULT)

    def _do_walk() -> tuple[list[dict], bool]:
        try:
            st = os.stat(resolved)
        except FileNotFoundError:
            raise OpError(ErrorCode.ENOENT, f"No such file or directory: {raw_path!r}")
        except OSError as exc:
            raise _os_err_to_op_error(exc, raw_path)
        if not _stat_mod.S_ISDIR(st.st_mode):
            raise OpError(ErrorCode.ENOTDIR, f"Not a directory: {raw_path!r}")
        entries, cap_hit, deadline_hit, _pending = _walk_tree(
            resolved, max_entries, _time.monotonic() + timeout_s
        )
        return entries, cap_hit or deadline_hit

    entries, truncated = await asyncio.to_thread(_do_walk)
    return {"entries": entries, "truncated": truncated}


def _read_capped(fd: int, limit: int) -> bytes | None:
    """Up to *limit* bytes of the open file, or ``None`` for a binary file (a NUL in its first
    ``_BINARY_SNIFF_BYTES``). Chunks are joined once, not concatenated one by one."""
    parts: list[bytes] = []
    total = 0
    while total < limit:
        chunk = os.read(fd, min(1 << 20, limit - total))
        if not chunk:
            break
        if total < _BINARY_SNIFF_BYTES and b"\x00" in (chunk[: _BINARY_SNIFF_BYTES - total]):
            return None
        parts.append(chunk)
        total += len(chunk)
    return b"".join(parts)


async def search(
    args: dict, workspace_root: str, locks: WorkspaceLockTable | None = None
) -> dict:
    """``search`` op (protocol 1.8) → ``{lines, match_count, file_count, truncated, stop_reason, stop_reasons, skipped_large}``; read-only, takes no lock.

    One recursive regex search in the runtime instead of one walk plus one
    ``read_file`` round-trip per file (review 2026-11-23, 5.11). The pattern
    is matched with the ``regex`` library (the same engine the platform
    validates it with), every match under a per-line ``timeout=`` and with
    the GIL released (``concurrent=True``), so a catastrophic pattern ends
    that search with ``stop_reason: regex_timeout`` and never freezes the
    runtime's event loop; stdlib ``re`` holds the GIL for the whole of one
    match. A pattern that does not compile is an EPROTOCOL refusal. Output
    lines are shaped exactly like the platform's ``grep_text``, with paths
    RELATIVE TO THE WORKSPACE ROOT (so the agent can pass one to ``read``):
    ``files_with_matches`` is one path per file, ``count`` is ``path:hits``
    (hits = matching LINES), ``content`` is ``path:lineno:line`` with
    ``context`` merged, and multiline ``content`` is ``path::snippet``
    (newlines escaped). ``match_count`` counts the same thing per file
    (lines, or whole-file matches in multiline mode) and ``file_count`` the
    files with at least one.

    Caps. ``stop_reasons`` lists every one that fired (``stop_reason`` is
    the first) and ``truncated`` is true when any did: ``size_cap`` (a file
    bigger than ``max_file_bytes`` is skipped, counted in ``skipped_large``,
    and the search continues), ``file_cap`` (at most ``max_files`` regular
    files that PASS the ``glob`` filter are even listed: the filter runs inside
    the walk, before the count; the walk is breadth-first in sorted order -
    flagged only when the cap left files UNVISITED, i.e. a file was actually
    dropped; a tree of exactly ``max_files`` fills the cap on its last entry
    and is not flagged),
    ``match_cap`` (at most ``max_matches`` matches are counted),
    ``time_cap`` (a ``timeout_s`` deadline), ``regex_timeout`` (one line's
    match ran past the per-line bound), and ``output_cap`` (the emitted
    lines would exceed ``max_output_bytes``). ``head_limit`` cuts the lines
    (``truncated`` only); both bound the response BEFORE it is sent, so a
    content search over minified one-line files cannot exceed the transport
    frame limit. Counting continues past a line cut, so ``match_count`` is
    the same as the local backend reports.
    """
    raw_path: str = args.get("path", "")
    resolved = _resolve_safe(raw_path, workspace_root)
    pattern: str = args.get("pattern") or ""
    if not isinstance(pattern, str) or not pattern:
        raise OpError(ErrorCode.EPROTOCOL, "search: 'pattern' must be a non-empty string")
    glob_filter: str | None = args.get("glob")
    if glob_filter is not None and not isinstance(glob_filter, str):
        raise OpError(ErrorCode.EPROTOCOL, f"glob must be a string, got {glob_filter!r}")
    output_mode: str = args.get("output_mode") or "files_with_matches"
    if output_mode not in ("files_with_matches", "count", "content"):
        raise OpError(
            ErrorCode.EPROTOCOL,
            f"output_mode must be files_with_matches, count or content, got {output_mode!r}",
        )
    context: Any = args.get("context")
    if context is None:
        context = 0
    if isinstance(context, bool) or not isinstance(context, int) or context < 0:
        raise OpError(ErrorCode.EPROTOCOL, f"context must be a non-negative integer, got {context!r}")
    multiline: bool = bool(args.get("multiline", False))
    case_insensitive: bool = bool(args.get("case_insensitive", False))
    skip_raw = args.get("skip") or []
    if not isinstance(skip_raw, list) or not all(isinstance(s, str) for s in skip_raw):
        raise OpError(ErrorCode.EPROTOCOL, f"skip must be a list of strings, got {skip_raw!r}")
    skip: tuple[str, ...] = tuple(skip_raw)
    max_files = _int_arg(args.get("max_files"), "max_files", _SEARCH_MAX_FILES_DEFAULT)
    max_file_bytes = _int_arg(
        args.get("max_file_bytes"), "max_file_bytes", _SEARCH_MAX_FILE_BYTES_DEFAULT
    )
    max_matches = _int_arg(args.get("max_matches"), "max_matches", _SEARCH_MAX_MATCHES_DEFAULT)
    max_output_bytes = _int_arg(
        args.get("max_output_bytes"), "max_output_bytes", _SEARCH_MAX_OUTPUT_BYTES_DEFAULT
    )
    head_limit: int | None = (
        _int_arg(args["head_limit"], "head_limit", 1) if args.get("head_limit") is not None else None
    )
    timeout_s = _float_arg(args.get("timeout_s"), "timeout_s", _OP_TIMEOUT_S_DEFAULT)

    flags = 0
    if case_insensitive:
        flags |= regex.IGNORECASE
    if multiline:
        flags |= regex.DOTALL | regex.MULTILINE
    try:
        rx = regex.compile(pattern, flags)
    except (regex.error, OverflowError, RecursionError, MemoryError) as exc:
        raise OpError(
            ErrorCode.EPROTOCOL,
            f"search: pattern does not compile: {exc}",
        ) from None

    # The workspace-relative path of the search root: output paths and the
    # reserved-tree skip are both relative to the WORKSPACE, not to *path*.
    try:
        base_rel = resolved.relative_to(pathlib.Path(workspace_root).resolve()).as_posix()
    except ValueError:
        base_rel = ""
    if base_rel == ".":
        base_rel = ""

    def _do_search() -> dict:
        deadline = _time.monotonic() + timeout_s
        reasons: list[str] = []

        def note(reason: str) -> None:
            if reason not in reasons:
                reasons.append(reason)

        if os.path.isdir(resolved):
            file_entries, cap_hit, deadline_hit, pending_at_cap = _walk_tree(
                resolved, max_files, deadline, skip=skip, files_only=True, skip_prefix=base_rel,
                file_filter=(
                    None if glob_filter is None
                    else (lambda rel: _glob_filter_matches(rel, glob_filter))
                ),
            )
            candidates: list[str] = [e["path"] for e in file_entries]
            if cap_hit and pending_at_cap:
                # The cap dropped a file (entries were left unvisited): a tree of
                # exactly max_files fills the cap on its last entry and is NOT cut.
                note("file_cap")
            if deadline_hit:
                note("time_cap")
        else:
            # A file target searches the file itself (the platform sends
            # directories, but a direct file stays a one-file search).
            try:
                if not os.path.isfile(resolved):
                    raise OpError(ErrorCode.ENOENT, f"No such file or directory: {raw_path!r}")
            except OpError:
                raise
            except OSError as exc:
                raise _os_err_to_op_error(exc, raw_path)
            candidates = [""]

        out_lines: list[str] = []
        out_bytes = 0
        lines_cut = False
        match_count = 0
        file_count = 0
        skipped_large = 0
        halt = False

        def emit(line: str) -> None:
            nonlocal out_bytes, lines_cut
            if lines_cut:
                return
            if head_limit is not None and len(out_lines) >= head_limit:
                lines_cut = True
                return
            size = len(line.encode("utf-8", errors="replace")) + 1
            if out_bytes + size > max_output_bytes:
                lines_cut = True
                note("output_cap")
                return
            out_lines.append(line)
            out_bytes += size

        def one_search_timeout() -> float:
            return min(_SEARCH_LINE_TIMEOUT_S, max(deadline - _time.monotonic(), 0.001))

        for rel in candidates:
            if halt:
                break
            if _time.monotonic() > deadline:
                note("time_cap")
                break
            if rel:
                rel_out = f"{base_rel}/{rel}" if base_rel else rel
            else:
                rel_out = base_rel
            if glob_filter is not None and not _glob_filter_matches(
                rel or resolved.name, glob_filter
            ):
                continue
            path = resolved if rel == "" else resolved / rel
            try:
                fd = os.open(
                    path,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0),
                )
            except OSError:
                continue  # a symlink swapped in, or a file gone: skip as the per-file read did
            try:
                st = os.fstat(fd)
                if not _stat_mod.S_ISREG(st.st_mode):
                    continue  # a FIFO or device swapped in after the walk: never read it
                if st.st_size > max_file_bytes:
                    skipped_large += 1
                    note("size_cap")
                    continue  # skip this file, keep searching the rest
                body = _read_capped(fd, max_file_bytes)
            except OSError:
                continue
            finally:
                os.close(fd)
            if body is None:
                continue
            text = body.decode("utf-8", errors="replace")

            if multiline:
                matches: list = []
                try:
                    for m in rx.finditer(text, timeout=one_search_timeout(), concurrent=True):
                        matches.append(m)
                        if match_count + len(matches) >= max_matches:
                            break
                except TimeoutError:
                    note("time_cap" if _time.monotonic() > deadline else "regex_timeout")
                    halt = True
                if match_count + len(matches) >= max_matches:
                    note("match_cap")
                    halt = True
                if not matches:
                    continue
                if output_mode == "files_with_matches":
                    emit(rel_out)
                elif output_mode == "count":
                    emit(f"{rel_out}:{len(matches)}")
                else:
                    for m in matches:
                        emit(f"{rel_out}::{m.group(0).replace(chr(10), chr(92) + 'n')}")
                match_count += len(matches)
                file_count += 1
                continue

            all_lines = text.splitlines()
            hits: list[int] = []
            for index, line in enumerate(all_lines):
                if _time.monotonic() > deadline:
                    note("time_cap")
                    halt = True
                    break
                try:
                    found = (
                        rx.search(line, timeout=one_search_timeout(), concurrent=True) is not None
                    )
                except TimeoutError:
                    note("time_cap" if _time.monotonic() > deadline else "regex_timeout")
                    halt = True
                    break
                if found:
                    hits.append(index)
                    if match_count + len(hits) >= max_matches:
                        note("match_cap")
                        halt = True
                        break
            if not hits:
                continue
            match_count += len(hits)
            file_count += 1
            if output_mode == "files_with_matches":
                emit(rel_out)
            elif output_mode == "count":
                emit(f"{rel_out}:{len(hits)}")
            else:
                emitted: set[int] = set()
                for index in hits:
                    start = max(0, index - context)
                    end = min(len(all_lines), index + 1 + context)
                    for i in range(start, end):
                        if i in emitted:
                            continue
                        emitted.add(i)
                        emit(f"{rel_out}:{i + 1}:{all_lines[i]}")

        return {
            "lines": out_lines,
            "match_count": match_count,
            "file_count": file_count,
            "truncated": bool(reasons) or lines_cut,
            "stop_reason": reasons[0] if reasons else None,
            "stop_reasons": reasons,
            "skipped_large": skipped_large,
        }

    return await asyncio.to_thread(_do_search)


async def write_file_cas(args: dict, workspace_root: str, locks: WorkspaceLockTable) -> dict:
    """``write_file_cas`` op (protocol 1.8) → ``{applied}``; Tier-A locked exactly like ``write_file``.

    Compare-and-swap write: under the SAME write lock ``write_file`` holds,
    the current bytes are streamed through SHA-256 (``O_NOFOLLOW``, so a
    symbolic link swapped in after the path was resolved is refused, not
    followed) and ``content_b64`` is written only if the digest equals
    ``expected_sha256`` (the hex digest of the bytes the caller read; the
    caller sends a hash, not the whole old file). A mismatch leaves the file
    untouched and answers ``applied: false`` (a signal for the caller to
    re-read and retry, NOT an error); a missing file is ENOENT. This makes
    the sandbox ``edit`` tool's read-modify-write atomic against concurrent
    edits (review 2026-11-23, 5.9).
    """
    raw_path: str = args.get("path", "")
    expected_sha256 = args.get("expected_sha256", "")
    content_b64: str = args.get("content_b64", "")
    mode: int | None = args.get("mode")
    resolved = _resolve_safe(raw_path, workspace_root)
    if (
        not isinstance(expected_sha256, str)
        or len(expected_sha256) != 64
        or any(c not in "0123456789abcdefABCDEF" for c in expected_sha256)
    ):
        raise OpError(ErrorCode.EPROTOCOL, "expected_sha256 must be a 64-character hex digest")
    expected_digest = expected_sha256.lower()
    try:
        content = base64.b64decode(content_b64)
    except Exception as exc:
        raise OpError(ErrorCode.EPROTOCOL, f"Invalid base64 for content_b64: {exc}")

    def _cas() -> bool:
        try:
            fd = os.open(
                resolved, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            )
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise OpError(ErrorCode.EACCES, f"A symbolic link was swapped in and is not followed: {raw_path!r}")
            raise _os_err_to_op_error(exc, raw_path)
        digest = hashlib.sha256()
        try:
            while chunk := os.read(fd, 1 << 20):
                digest.update(chunk)
        except OSError as exc:
            raise _os_err_to_op_error(exc, raw_path)
        finally:
            os.close(fd)
        if digest.hexdigest() != expected_digest:
            return False
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_bytes(resolved, content, mode)
        except OSError as exc:
            raise _os_err_to_op_error(exc, raw_path)
        return True

    _t0 = _time.monotonic()
    async with locks.hold_write(_scope_key(resolved, workspace_root), str(resolved)):
        waited = _time.monotonic() - _t0
        if waited > _LOCK_WAIT_TRACE_S:
            log.debug("write_file_cas waited %.3fs on busy lock for %r", waited, raw_path)
        applied = await asyncio.to_thread(_cas)
    return {"applied": applied}


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
    except TimeoutError:
        await _stop_git(proc)
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
        except TimeoutError:
            await _stop_git(proc2)
            raise OpError(
                ErrorCode.EINTERNAL,
                f"git config timed out after {timeout}s",
            )


async def state_commit(
    args: dict, workspace_root: str, locks: WorkspaceLockTable | None = None
) -> dict:
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

    def _write_files() -> list[str]:
        """Write files to disk and collect paths to stage."""
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
        return staged

    async def _commit() -> dict:
        # One commit at a time per repo across every connection (GI-5): the platform's commit lock is per process, and
        # two commits interleaving here failed on git's locks or took in each other's files.
        async with (locks or _STATE_LOCKS).hold_scope(os.path.realpath(state_dir)):
            await _ensure_state_repo(state_dir)
            staged = await asyncio.to_thread(_write_files)

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

    # Shielded: a commit whose connection closed still completes (as it did when it ran inline), so its staged files
    # never ride into the next commit under another session's trailers.
    task = asyncio.ensure_future(_commit())
    _COMMITS.add(task)
    task.add_done_callback(_commit_done)
    return await asyncio.shield(task)


# The server passes its app-wide table (shared by every connection); a direct caller passes none and gets this one.
_STATE_LOCKS = WorkspaceLockTable()

# Strong references to the commits in flight (see ``state_commit``): the loop keeps only weak ones to its tasks.
_COMMITS: set[asyncio.Task[dict]] = set()


def _commit_done(task: asyncio.Task[dict]) -> None:
    _COMMITS.discard(task)
    if not task.cancelled():
        task.exception()  # retrieved: nobody awaits a commit whose connection closed


async def state_read(
    args: dict, workspace_root: str, locks: WorkspaceLockTable | None = None
) -> dict:
    """``state_read`` op: read files from <workspace_root>/.state by path.

    args:
      paths  list[str]  -- relative paths to read

    Returns: {"files": {path: content_b64_or_null}}
    where each value is a base64 string if the file exists, or null if absent.
    """
    paths: list[str] = args.get("paths") or []
    state_dir = pathlib.Path(workspace_root) / ".state"

    def _read() -> dict[str, str | None]:
        result: dict[str, str | None] = {}
        for rel_path in paths:
            target = state_dir / rel_path
            if target.exists() and target.is_file():
                content = target.read_bytes()
                result[rel_path] = base64.b64encode(content).decode()
            else:
                result[rel_path] = None
        return result

    return {"files": await asyncio.to_thread(_read)}  # off the loop (GI-10)


async def state_history(
    args: dict, workspace_root: str, locks: WorkspaceLockTable | None = None
) -> dict:
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
    if args.get("with_files"):
        from primer_runtime.state_inspect import state_history_files
        return await state_history_files(args, workspace_root)

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

from primer_runtime.state_inspect import state_show_commit


HANDLERS: dict[str, object] = {
    "read_file": read_file,
    "capture_service_bundle": capture_service_bundle,
    "write_file": write_file,
    "append_line": append_line,
    "list_dir": list_dir,
    "stat": stat,
    "delete": delete,
    "walk": walk,
    "search": search,
    "write_file_cas": write_file_cas,
    "state_commit": state_commit,
    "state_read": state_read,
    "state_history": state_history,
    "state_show_commit": state_show_commit,
}


# ---------------------------------------------------------------------------
# Single-shot-op-as-task: keep the runtime message loop free while a writer
# parks on a busy lock or a long op runs (mirrors exec.start_exec).
# ---------------------------------------------------------------------------


class MutatingOpRegistry:
    """Per-connection set of in-flight mutating-op tasks.

    ``write_file``/``append_line``/``delete`` acquire a Tier-A write lock and
    may PARK on a busy scope/path lock. The server spawns each as a tracked
    task (mirrors :class:`~primer_runtime.exec.ExecRegistry`) so a parked
    writer never blocks the single runtime message loop. Every other
    single-shot op (reads, list, stat, the git state ops) runs the same way
    since GI-10, so HEALTH never waits behind one. ``cancel_all`` is invoked on
    WS close to tear down any op still in flight (a ``state_commit`` finishes
    behind its shield).
    """

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[None]] = set()

    def add(self, task: asyncio.Task[None]) -> None:
        self._tasks.add(task)

    def discard(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)

    def cancel_all(self) -> None:
        """Cancel every in-flight mutating-op task (called on WS close)."""
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()


async def _run_mutating_op(
    handler: Callable[..., Coroutine[Any, Any, dict]],
    op_name: str,
    req_id: int,
    args: dict,
    workspace_root: str,
    locks: WorkspaceLockTable,
    send: Callable[[str], Coroutine[Any, Any, None]],
) -> None:
    """Task body: await a mutating handler off the loop, send ONE Response.

    Preserves the inline single-shot framing exactly - an ``ok=true`` Response
    with the handler result, an ``OpError`` mapped to ``ok=false`` with its
    code, and any other exception mapped to ``EINTERNAL`` - keyed by *req_id*
    so response multiplexing is unchanged. The only difference is that this
    runs as its own task, so awaiting a busy Tier-A lock never parks the
    message loop.
    """
    try:
        result = await handler(args, workspace_root, locks)
        await send(serialize(Response(req_id=req_id, ok=True, result=result)))
    except OpError as exc:
        await send(serialize(Response(
            req_id=req_id, ok=False,
            error={"code": exc.code, "message": exc.message},
        )))
    except asyncio.CancelledError:
        # WS close / cancel_all: propagate so the task ends promptly.
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("Unexpected error handling op %r", op_name)
        await send(serialize(Response(
            req_id=req_id, ok=False,
            error={"code": ErrorCode.EINTERNAL, "message": str(exc)},
        )))


def start_mutating_op(
    handler: Callable[..., Coroutine[Any, Any, dict]],
    op_name: str,
    req_id: int,
    args: dict,
    workspace_root: str,
    locks: WorkspaceLockTable,
    send: Callable[[str], Coroutine[Any, Any, None]],
    registry: MutatingOpRegistry,
) -> asyncio.Task[None]:
    """Spawn a tracked mutating-op task; returns it (tests await it).

    Registered in *registry* so ``cancel_all`` can reach it on WS close; a
    done-callback deregisters it on completion.
    """
    task = asyncio.create_task(
        _run_mutating_op(handler, op_name, req_id, args, workspace_root, locks, send),
        name=f"{op_name}:{req_id}",
    )
    registry.add(task)
    task.add_done_callback(registry.discard)
    return task
