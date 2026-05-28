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

from matrix_runtime.protocol import ErrorCode


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
# Dispatch table
# ---------------------------------------------------------------------------

HANDLERS: dict[str, object] = {
    "read_file": read_file,
    "write_file": write_file,
    "append_line": append_line,
    "list_dir": list_dir,
    "stat": stat,
    "delete": delete,
}
