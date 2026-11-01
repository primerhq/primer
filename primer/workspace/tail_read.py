"""Reading the end of a log file instead of the whole of it (PERF-T14 stage 3).

``messages.jsonl`` only grows, and the readers that decide something about a session (the relay's final text, whether a turn is open) look
at its last window. A reader hands :func:`read_window` a ``read_tail(max_bytes) -> (bytes, is_whole_file) | None`` and a test for "this
tail already holds what I need"; the tail is widened (x4) until the test passes or the whole file has been read.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

#: The first tail read; a window of a few turns is well inside it.
TAIL_START_BYTES = 64 * 1024

ReadTail = Callable[[int], Awaitable["tuple[bytes, bool] | None"]]


async def tail_of(workspace: Any, path: str, max_bytes: int) -> tuple[bytes, bool] | None:
    """The last ``max_bytes`` of ``path`` and whether that is the whole file, or ``None`` when ``workspace`` cannot read a range.

    Uses ``read_file_tail`` when the backend has it (the local one), else a size (``file_info`` or ``stat``) and the ranged ``read_file_from``
    (the sandbox and runtime backends). Raises like ``read_file`` (``NotFoundError`` for an absent file).
    """
    tail = getattr(workspace, "read_file_tail", None)
    if tail is not None:
        return await tail(path, max_bytes)
    ranged = getattr(workspace, "read_file_from", None)
    if ranged is None:
        return None
    size = None
    file_info = getattr(workspace, "file_info", None)
    stat = getattr(workspace, "stat", None)
    if file_info is not None:
        size = (await file_info(path)).size_bytes
    elif stat is not None:
        info = await stat(path)
        if info is None:
            from primer.model.except_ import NotFoundError

            raise NotFoundError(f"{path!r} not found")
        size = info.size_bytes
    if size is None:
        return None
    if size <= max_bytes:
        return await workspace.read_file(path), True
    return await ranged(path, size - max_bytes), False


def whole_lines(data: bytes, whole: bool) -> list[bytes]:
    """The ``\\n``-split lines of a tail; a tail that starts mid-file drops its first (possibly cut) line."""
    lines = data.split(b"\n")
    return lines if whole else lines[1:]


async def read_window(
    read_tail: ReadTail, enough: Callable[[list[bytes]], bool], *, start: int = TAIL_START_BYTES,
) -> bytes | None:
    """The end of a log, widened until ``enough(lines)`` holds or it is the whole file; ``None`` when the log cannot be read by range.

    ``enough`` sees the complete lines of the tail (a cut first line is not among them). The returned bytes start at a line boundary
    (the cut first line is dropped) unless they are the whole file.
    """
    size = start
    while True:
        got = await read_tail(size)
        if got is None:
            return None
        data, whole = got
        if whole:
            return data
        lines = whole_lines(data, False)
        if enough(lines):
            return b"\n".join(lines)
        size *= 4
