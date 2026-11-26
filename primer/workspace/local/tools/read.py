"""``read`` -- read a file with offset/limit pagination."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from primer.model.chat import ToolExample
from primer.model.except_ import BadRequestError, NotFoundError
from primer.workspace.tool import ToolCallContext, ToolResult, WorkspaceTool
from primer.workspace.local.tools._common import (
    TOOL_RESULT_CAP_BYTES,
    resolve_workspace_path,
)


_BINARY_SNIFF_BYTES = 1024
_DEFAULT_LIMIT = 2000
#: Character bound per ``readline`` call: one call decodes at most this
#: many characters, so a single call's memory is bounded.
_LINE_READ_CHARS = TOOL_RESULT_CAP_BYTES


class ReadArgs(BaseModel):
    """Arguments for the ``read`` tool."""

    path: str = Field(..., description="File path relative to the workspace root.")
    offset: int = Field(
        default=0,
        ge=0,
        description="Line number to start from (0-indexed).",
    )
    limit: int | None = Field(
        default=_DEFAULT_LIMIT,
        ge=1,
        description="Max lines to return. Pass null for no cap.",
    )


class Read(WorkspaceTool):
    """Read a file with offset/limit pagination.

    Output: each line prefixed with its 1-based line number in
    ``%6d→%s`` format. Binary files (detected by NUL-byte sniff) emit
    a stable summary string instead of raw bytes.

    The tool sets :attr:`ToolResult.truncated` to True whenever it
    returns less than the full file (either because the binary sniff
    fired or because the offset/limit window doesn't cover the rest).
    The runtime then honours that flag and skips outer truncation.

    Any window of any file is served with bounded memory and a bounded
    result: the file is read in text mode (universal newlines, so CRLF
    and a lone CR are line breaks, and invalid bytes are replaced) in
    ``readline`` calls bounded to ``TOOL_RESULT_CAP_BYTES`` characters,
    so the whole file is never loaded and no single call buffers past
    the bound. Lines are skipped to ``offset``, collected up to
    ``limit``, and the returned text stops before it would exceed
    ``TOOL_RESULT_CAP_BYTES`` (50 KiB, the same cap the runtime's outer
    truncation applies to any tool result), measured in ENCODED bytes --
    so non-ASCII text and invalid UTF-8 (a replaced byte is up to 3
    encoded bytes) cannot return more than the cap plus the notes, and
    a line whose head already outruns the cap is served cut at its head
    (within whatever room the window has left), with a note naming the
    cut. Text-mode ``readline`` splits only on ``\\n``, ``\\r\\n`` and a
    lone ``\\r``: unlike ``str.splitlines()`` it does NOT split on
    ``\\x0c``, ``\\x1c``, ``\\x85`` or ``\\u2028``. Time is proportional
    to the offset; memory is bounded by the cap.
    """

    read_only: ClassVar[bool] = True
    self_bounded: ClassVar[bool] = True

    id: ClassVar[str] = "read"
    description: ClassVar[str] = (
        "Read a file with offset+limit pagination; output prefixes each "
        "line with its number, and binary files return a summary.\n\n"
        "Use when you need the contents of a known file path; not for "
        "finding files (use ``glob``) or searching contents (use "
        "``grep``)."
    )
    examples: ClassVar[list[ToolExample]] = [
        ToolExample(args={"path": "README.md"}, returns="numbered lines 1..N"),
        ToolExample(
            args={"path": "src/big.py", "offset": 100, "limit": 50},
            returns="lines 101-150",
        ),
    ]

    def __init__(self, workspace_root: Path) -> None:
        self._root = Path(workspace_root)

    def parameters(self) -> type[BaseModel]:
        return ReadArgs

    async def execute(self, args: BaseModel, ctx: ToolCallContext) -> ToolResult:
        assert isinstance(args, ReadArgs)
        target = resolve_workspace_path(self._root, args.path)
        if not target.exists():
            raise NotFoundError(f"{args.path!r} not found")
        if not target.is_file():
            raise BadRequestError(f"{args.path!r} is not a file")

        # Binary sniff first: avoid loading a 2 GB binary into memory.
        sample = await asyncio.to_thread(_read_head_bytes, target, _BINARY_SNIFF_BYTES)
        if _looks_binary(sample):
            size = await asyncio.to_thread(lambda: target.stat().st_size)
            ctx.session.mark_read(args.path)
            return ToolResult(
                output=f"<binary file: {size} bytes>",
                truncated=True,
            )

        lines, cut, had_more, line_cut = await asyncio.to_thread(
            _read_window, target, args.offset, args.limit
        )
        formatted = "\n".join(lines)
        ctx.session.mark_read(args.path)

        truncated = cut or args.offset > 0 or had_more
        if cut:
            output = formatted + (
                f"\n[window cut at {TOOL_RESULT_CAP_BYTES} bytes: "
                f"{len(lines)} lines shown]"
            )
            if line_cut:
                output += (
                    f"\n[last line cut: it is longer than "
                    f"{TOOL_RESULT_CAP_BYTES} bytes; only its head is shown]"
                )
        else:
            output = formatted
        return ToolResult(output=output, truncated=truncated)


def _read_head_bytes(path: Path, n: int) -> bytes:
    with path.open("rb") as fh:
        return fh.read(n)


def _read_window(
    path: Path, offset: int, limit: int | None
) -> tuple[list[str], bool, bool, bool]:
    """Serve the ``[offset, offset+limit)`` window of ``path`` with
    bounded memory and a bounded result.

    The file is read in text mode (``errors="replace"``, universal
    newlines: CRLF and a lone CR are line breaks, as in the original
    reader) and consumed with ``readline`` bounded to
    ``_LINE_READ_CHARS`` characters per call, so the whole file is never
    loaded and no single call buffers more than the bound. Lines are
    skipped to ``offset`` (a skipped line is consumed to its end in
    bounded reads and kept out of memory), collected up to ``limit`` --
    a line that exists past the window sets the pagination flag instead
    of being served -- and the returned text stops, with a truncation
    note, before it would exceed ``TOOL_RESULT_CAP_BYTES`` measured in
    ENCODED bytes, so non-ASCII text and invalid UTF-8 (a replaced byte
    is up to 3 encoded bytes) cannot return more than the cap plus the
    notes. A line whose head already outruns the cap in encoded bytes is
    served cut at its head (within the room the window has left), with
    a note naming the cut, instead of read whole. Time is proportional
    to the offset; memory is bounded by the cap. Returns
    ``(formatted_lines, cut, had_more, line_cut)``: ``cut``
    says the window or a line was cut by the byte cap, ``had_more`` says
    the file continues past what was returned, ``line_cut`` says the
    last line shown was served cut at its head.
    """
    formatted_lines: list[str] = []
    size = 0
    cut = False
    had_more = False
    line_cut = False
    idx = -1
    with path.open(encoding="utf-8", errors="replace", newline=None) as fh:
        while True:
            piece = fh.readline(_LINE_READ_CHARS)
            if not piece:
                break  # EOF: a final line, if any, was already read
            idx += 1
            if not piece.endswith("\n"):
                if idx < offset:
                    # A skipped line keeps nothing: consume it to its end
                    # (or EOF) in bounded reads, one piece at a time.
                    while True:
                        nxt = fh.readline(_LINE_READ_CHARS)
                        if not nxt or nxt.endswith("\n"):
                            break
                else:
                    # A line in the window: accumulate until its newline
                    # (or EOF), stopping as soon as its ENCODED head
                    # outruns the cap -- no more of it could ever fit.
                    enc = len(piece.encode("utf-8"))
                    while enc <= TOOL_RESULT_CAP_BYTES and not piece.endswith("\n"):
                        nxt = fh.readline(_LINE_READ_CHARS)
                        if not nxt:
                            break
                        piece += nxt
                        enc += len(nxt.encode("utf-8"))
            if idx < offset:
                continue
            if limit is not None and len(formatted_lines) >= limit:
                # A line exists past the window: the pagination flag,
                # decided from the line itself, not assumed.
                had_more = True
                break
            line = piece[:-1] if piece.endswith("\n") else piece
            numbered = f"{idx + 1:>6}\u2192{line}"
            room = TOOL_RESULT_CAP_BYTES - size - (1 if formatted_lines else 0)
            numbered_size = len(numbered.encode("utf-8"))
            if numbered_size <= room:
                if formatted_lines:
                    size += 1  # the "\n" that joins this line
                formatted_lines.append(numbered)
                size += numbered_size
            else:
                # The line would push the window past the cap: show the
                # part of it that fits the REMAINING room (cut on a
                # character boundary) and stop, named as cut. A line
                # that is itself over the cap also gets the "last line
                # cut" note, when any of its head is shown.
                part = _cut_encoded(numbered, room)
                if part:
                    formatted_lines.append(part)
                    line_cut = len(line.encode("utf-8")) > TOOL_RESULT_CAP_BYTES
                cut = True
                had_more = True
                break
    return formatted_lines, cut, had_more, line_cut


def _cut_encoded(s: str, budget: int) -> str:
    """The longest character prefix of ``s`` whose UTF-8 encoding is at
    most ``budget`` bytes ("" for a non-positive budget), so the cut
    never splits a multi-byte character."""
    if budget <= 0:
        return ""
    if len(s.encode("utf-8")) <= budget:
        return s
    lo, hi = 0, len(s)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(s[:mid].encode("utf-8")) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return s[:lo]


def _looks_binary(sample: bytes) -> bool:
    """Heuristic: NUL byte in the first chunk = binary."""
    return b"\x00" in sample


__all__ = ["Read", "ReadArgs"]
