"""SandboxTruncationStore -- truncation cache inside a Sandbox.

Same contract as :class:`matrix.workspace.local.cache.LocalTruncationStore`
(``output`` / ``write`` / ``cleanup`` / ``cleanup_session``) but writes
+ reads go through the sandbox via argv-form file ops. The filename
pattern ``tool_<nanos>_<counter>.txt`` is identical so callers can read
paths back via the read tool unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import timedelta
from typing import Literal

from matrix.int.sandbox import Sandbox
from matrix.workspace.local.cache import TruncatedOutput  # reuse the model


logger = logging.getLogger(__name__)


_DEFAULT_MAX_LINES = 2000
_DEFAULT_MAX_BYTES = 50 * 1024
_DEFAULT_RETENTION = timedelta(days=7)
_FILENAME_PATTERN = re.compile(r"^tool_(\d+)_(\d+)\.txt$")


def _validate_session_id(session_id: str) -> None:
    if not session_id:
        raise ValueError("session_id must be non-empty")
    if "/" in session_id or "\\" in session_id or session_id in (".", ".."):
        raise ValueError(
            f"session_id contains illegal characters: {session_id!r}"
        )
    if "\x00" in session_id:
        raise ValueError("session_id contains a null byte")


class SandboxTruncationStore:
    """Per-workspace truncation cache, dispatched through a Sandbox."""

    def __init__(
        self,
        sandbox: Sandbox,
        *,
        root: str,
        max_lines: int = _DEFAULT_MAX_LINES,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        retention: timedelta = _DEFAULT_RETENTION,
    ) -> None:
        if max_lines < 1:
            raise ValueError("max_lines must be >= 1")
        if max_bytes < 1:
            raise ValueError("max_bytes must be >= 1")
        if retention.total_seconds() <= 0:
            raise ValueError("retention must be positive")
        self._sandbox = sandbox
        self._root = root
        self._max_lines = max_lines
        self._max_bytes = max_bytes
        self._retention = retention
        self._counter = 0
        self._counter_lock = asyncio.Lock()

    @property
    def root(self) -> str:
        return self._root

    async def output(
        self,
        text: str,
        *,
        session_id: str,
        max_lines: int | None = None,
        max_bytes: int | None = None,
        direction: Literal["head", "tail"] = "head",
    ) -> TruncatedOutput:
        _validate_session_id(session_id)
        line_limit = max_lines if max_lines is not None else self._max_lines
        byte_limit = max_bytes if max_bytes is not None else self._max_bytes
        if line_limit < 1:
            raise ValueError("max_lines must be >= 1")
        if byte_limit < 1:
            raise ValueError("max_bytes must be >= 1")

        encoded = text.encode("utf-8")
        line_count = text.count("\n") + (
            0 if text.endswith("\n") or text == "" else 1
        )
        if len(encoded) <= byte_limit and line_count <= line_limit:
            return TruncatedOutput(
                content=text, truncated=False, output_path=None,
            )

        path = await self.write(text, session_id=session_id)
        preview = _build_preview(
            text,
            line_limit=line_limit, byte_limit=byte_limit,
            direction=direction, path=path,
        )
        return TruncatedOutput(content=preview, truncated=True, output_path=path)

    async def write(self, text: str, *, session_id: str) -> str:
        _validate_session_id(session_id)
        filename = await self._next_filename()
        path = f"{self._root}/{session_id}/{filename}"
        await self._sandbox.write_file(path, text.encode("utf-8"))
        return path

    async def cleanup(self) -> int:
        """Walk session subdirs and delete files past retention."""
        cutoff_nanos = time.time_ns() - int(
            self._retention.total_seconds() * 1_000_000_000
        )
        root_info = await self._sandbox.stat(self._root)
        if root_info is None or root_info.kind != "dir":
            return 0
        removed = 0
        for entry in await self._sandbox.list_dir(self._root):
            if entry.kind != "dir":
                continue
            session_dir = f"{self._root}/{entry.path}"
            for fent in await self._sandbox.list_dir(session_dir):
                if fent.kind != "file":
                    continue
                match = _FILENAME_PATTERN.match(fent.path)
                if match is None:
                    continue
                file_nanos = int(match.group(1))
                if file_nanos < cutoff_nanos:
                    await self._sandbox.delete(f"{session_dir}/{fent.path}")
                    removed += 1
        return removed

    async def cleanup_session(self, session_id: str) -> int:
        """Remove this session's entire cache subdirectory."""
        _validate_session_id(session_id)
        session_dir = f"{self._root}/{session_id}"
        info = await self._sandbox.stat(session_dir)
        if info is None:
            return 0
        count = 0
        for fent in await self._sandbox.list_dir(session_dir):
            if fent.kind == "file" and _FILENAME_PATTERN.match(fent.path):
                count += 1
        await self._sandbox.delete(session_dir)
        return count

    async def _next_filename(self) -> str:
        async with self._counter_lock:
            self._counter += 1
            counter = self._counter
        nanos = time.time_ns()
        return f"tool_{nanos}_{counter:08d}.txt"


def _build_preview(
    text: str, *,
    line_limit: int, byte_limit: int,
    direction: Literal["head", "tail"],
    path: str,
) -> str:
    lines = text.splitlines(keepends=True)
    slice_lines = (
        lines[:line_limit] if direction == "head"
        else (lines[-line_limit:] if line_limit < len(lines) else lines)
    )
    preview = "".join(slice_lines)
    encoded = preview.encode("utf-8")
    if len(encoded) > byte_limit:
        if direction == "head":
            preview = encoded[:byte_limit].decode("utf-8", errors="ignore")
        else:
            preview = encoded[-byte_limit:].decode("utf-8", errors="ignore")
    hint = (
        f"\n\nThe tool call succeeded but the output was truncated.\n"
        f"Full output saved to: {path}\n"
        f"Use grep to search the full content or read with offset/limit "
        f"to view specific sections."
    )
    return preview + hint


__all__ = ["SandboxTruncationStore"]
