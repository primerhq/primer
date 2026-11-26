"""``read`` for the sandbox backend.

Serves any offset/limit window of any file with bounded memory and a
bounded result, the same contract the local ``read`` tool holds: the file
is paged through the sandbox's ``read_file_range`` (one bounded byte
window per round-trip; the runtime's ``read_file(limit)`` keeps each frame
small, so a big file cannot drop the workspace connection, review
2026-11-23, 5.12), decoded with ``errors="replace"``, split on ``\\n``,
``\\r\\n`` and a lone ``\\r`` (a trailing lone ``\\r`` at a chunk boundary is
deferred: it may be the first half of a ``\\r\\n`` pair), lines skipped to
``offset``, collected up to ``limit``, and cut with a note once the
returned text would exceed ``TOOL_RESULT_CAP_BYTES`` measured in ENCODED
bytes. A line longer than the cap is served cut at its head, with a note
naming the cut. Time is proportional to the offset; memory is bounded by
the cap plus one chunk.

A sandbox without ``read_file_range`` (an older Sandbox impl) gets the
legacy whole-file read.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

from primer.int.sandbox import Sandbox
from primer.model.chat import ToolExample
from primer.model.except_ import BadRequestError, NotFoundError
from primer.workspace.local.tools.read import (
    ReadArgs,
    _cut_encoded,
)
from primer.workspace.local.tools._common import TOOL_RESULT_CAP_BYTES
from primer.workspace.sandbox.tools._common import resolve_sandbox_path
from primer.workspace.tool import ToolCallContext, ToolResult, WorkspaceTool


_BINARY_SNIFF_BYTES = 1024
#: Bytes fetched per round-trip of the window read (a base64 frame of this
#: size is a fraction of the 256 MiB WebSocket frame cap).
_CHUNK_BYTES = 128 * 1024
#: Most bytes one UNFINISHED line may hold. A skipped line that outruns it
#: is discarded (only its newline is wanted); a window line that outruns it
#: is over the result cap by construction and is served cut at its head.
_PENDING_MAX = TOOL_RESULT_CAP_BYTES


class SandboxRead(WorkspaceTool):
    """Read a file with offset+limit pagination via Sandbox."""

    read_only: ClassVar[bool] = True
    self_bounded: ClassVar[bool] = True  # the window read cuts at TOOL_RESULT_CAP_BYTES itself

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

    def __init__(self, sandbox: Sandbox, *, workspace_root: str) -> None:
        self._sandbox = sandbox
        self._root = workspace_root

    def parameters(self) -> type[BaseModel]:
        return ReadArgs

    async def execute(
        self, args: BaseModel, ctx: ToolCallContext,
    ) -> ToolResult:
        assert isinstance(args, ReadArgs)
        target = resolve_sandbox_path(self._root, args.path)
        info = await self._sandbox.stat(target)
        if info is None:
            raise NotFoundError(f"{args.path!r} not found")
        if info.kind != "file":
            raise BadRequestError(f"{args.path!r} is not a file")

        range_read = getattr(self._sandbox, "read_file_range", None)
        if getattr(self._sandbox, "supports_range_read", True) is False:
            # A runtime below protocol 1.8 ignores the byte limit and sends
            # the whole file for every page: one whole-file read is cheaper
            # than paging it (the page count times the file size).
            range_read = None
        if range_read is None:
            # Legacy Sandbox impl, or a runtime without a bounded read: the
            # old whole-file path (kept so such a sandbox keeps working).
            return await self._read_whole_file(target, args, ctx)

        sniff = await range_read(target, 0, _BINARY_SNIFF_BYTES)
        if b"\x00" in sniff:
            ctx.session.mark_read(args.path)
            return ToolResult(
                output=f"<binary file: {info.size_bytes} bytes>",
                truncated=True,
            )

        lines, cut, had_more, line_cut = await self._read_window(
            range_read, target, info.size_bytes, args.offset, args.limit
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

    async def _read_whole_file(
        self, target: str, args: ReadArgs, ctx: ToolCallContext
    ) -> ToolResult:
        """The legacy whole-file read (a sandbox without read_file_range)."""
        body = await self._sandbox.read_file(target)
        if b"\x00" in body[:_BINARY_SNIFF_BYTES]:
            ctx.session.mark_read(args.path)
            return ToolResult(
                output=f"<binary file: {len(body)} bytes>",
                truncated=True,
            )

        text = body.decode("utf-8", errors="replace")
        lines = text.splitlines()
        end = (
            args.offset + args.limit if args.limit is not None
            else len(lines)
        )
        sliced = lines[args.offset:end]
        formatted = "\n".join(
            f"{args.offset + i + 1:>6}\u2192{line}"
            for i, line in enumerate(sliced)
        )
        ctx.session.mark_read(args.path)
        truncated = args.offset > 0 or end < len(lines)
        # The tool bounds its own result (self_bounded): cut the window at the cap, as the paged read does.
        if len(formatted.encode("utf-8")) > TOOL_RESULT_CAP_BYTES:
            formatted = _cut_encoded(formatted, TOOL_RESULT_CAP_BYTES)
            shown = formatted.count("\n") + 1
            formatted += f"\n[window cut at {TOOL_RESULT_CAP_BYTES} bytes: {shown} lines shown]"
            truncated = True
        return ToolResult(output=formatted, truncated=truncated)

    async def _read_window(
        self,
        range_read,
        target: str,
        size: int,
        offset: int,
        limit: int | None,
    ) -> tuple[list[str], bool, bool, bool]:
        """The ``[offset, offset + limit)`` line window of *target*.

        Fetches the file in ``_CHUNK_BYTES`` windows (memory bounded by one
        chunk plus one bounded line head) and applies the same windowing and
        capping rules the local read applies in text mode. Line breaks are
        the text-mode ones: ``\\n``, ``\\r\\n`` and a lone ``\\r`` -- NOT
        ``\\x0c``, ``\\x85`` or ``\\u2028``, so the line numbers match the
        local read and do not follow ``str.splitlines()`` (the bytes never
        decode before the break search, so the multi-byte ``\\u2028`` cannot
        be mistaken for a break either). Returns
        ``(formatted_lines, cut, had_more, line_cut)`` like the local
        ``_read_window``.
        """
        kept: list[str] = []
        size_acc = 0  # encoded bytes of the formatted output so far
        cut = False
        had_more = False
        line_cut = False
        line_no = 0  # completed lines so far (0-indexed)
        pending = b""  # the current unfinished line's retained head
        stop = False
        pos = 0

        def handle_line(line_bytes: bytes) -> None:
            """One completed line (its full content); advances the window."""
            nonlocal line_no, cut, had_more, line_cut, size_acc, stop
            text = line_bytes.decode("utf-8", errors="replace")
            if line_no < offset:
                line_no += 1
                return
            if limit is not None and len(kept) >= limit:
                had_more = True
                stop = True
                return
            numbered = f"{line_no + 1:>6}\u2192{text}"
            room = TOOL_RESULT_CAP_BYTES - size_acc - (1 if kept else 0)
            numbered_size = len(numbered.encode("utf-8"))
            if numbered_size <= room:
                if kept:
                    size_acc += 1  # the "\n" that joins this line
                kept.append(numbered)
                size_acc += numbered_size
            else:
                part = _cut_encoded(numbered, room)
                if part:
                    kept.append(part)
                line_cut = len(text.encode("utf-8")) > TOOL_RESULT_CAP_BYTES
                cut = True
                had_more = True
                stop = True
            line_no += 1

        def serve_head_and_stop(head: bytes) -> None:
            """A window line that outran the pending bound: its full content
            is over the cap by construction, so serve the head cut to the
            remaining room and stop (the local read does the same for a line
            whose head already outruns the cap)."""
            nonlocal line_no, cut, had_more, line_cut, size_acc, stop
            text = head.decode("utf-8", errors="replace").rstrip("\r")
            numbered = f"{line_no + 1:>6}\u2192{text}"
            room = TOOL_RESULT_CAP_BYTES - size_acc - (1 if kept else 0)
            part = _cut_encoded(numbered, room)
            if part:
                kept.append(part)
            line_cut = True
            cut = True
            had_more = True
            stop = True
            line_no += 1

        while not stop:
            if pos >= size:
                break
            chunk = await range_read(target, pos, min(_CHUNK_BYTES, size - pos))
            pos += len(chunk)
            if not chunk:
                # No more bytes (the file shrank after the stat): the
                # unfinished line, if any, is the final line, and nothing
                # follows. A pending of a lone deferred \r is the break of
                # the line already counted: drop it.
                if pending and pending != b"\r":
                    handle_line(pending)
                break
            if pending == b"\r":
                # The previous chunk ended on a deferred lone \r: the break of the line already counted (the deferral settles it). If this chunk starts with \n, it is the second half of a CRLF pair: consume it, then settle what is left as a fresh start.
                if chunk.startswith(b"\n"):
                    chunk = chunk[1:]
                pending = b""
                if not chunk:
                    if pos >= size:
                        # The file ended on the pair (the only remaining byte was the \n): nothing follows the counted line.
                        break
                    # The whole chunk was the pair's second half: fetch the next one.
                    continue
            data = pending + chunk
            eof = pos >= size

            # Settle every complete line in *data*; the tail (an unfinished
            # line, possibly an ambiguous trailing lone \r) carries on.
            if eof:
                found = True
                settled, pending = data, b""
                ends_with_break = data.endswith((b"\n", b"\r"))
            else:
                found = False
                j = len(data) - 1
                while j >= 0:
                    c = data[j]
                    if c == 0x0A:  # \n
                        cut_at, found = j + 1, True
                        break
                    if c == 0x0D:  # \r
                        if j + 1 < len(data) and data[j + 1] == 0x0A:
                            cut_at, found = j + 2, True  # a complete \r\n
                        elif j + 1 < len(data):
                            cut_at, found = j + 1, True  # a lone \r: a line break
                        else:
                            cut_at, found = j, True  # a trailing lone \r: may be half of a \r\n, defer it
                        break
                    j -= 1
                if not found:
                    settled, pending, ends_with_break = b"", data, False
                else:
                    settled, pending = data[:cut_at], data[cut_at:]
                    # The deferred trailing lone \r (pending == b"\r") is the break of the line BEFORE it: that line is complete, so the last settled segment is THAT line and must be settled, not popped as the start of a new one.
                    ends_with_break = pending != b"\r"

            # Bound the unfinished line's memory: a line that outruns
            # _PENDING_MAX is over the result cap if it is in the window
            # (serve its head and stop), or useless if it is being skipped
            # or is past a full window (discard it: the next break in the
            # stream ends the line).
            in_window = line_no >= offset and (limit is None or line_no < offset + limit)
            if pending and len(pending) > _PENDING_MAX and not eof:
                if in_window:
                    serve_head_and_stop(pending[:_PENDING_MAX])
                    stop = True
                    break
                pending = b""

            if found:
                # \n, \r\n and a lone \r are line breaks (as in the local
                # reader's text mode); a \r\n pair in the settled region is
                # always complete, so the normalisation cannot merge lines.
                # (settled may be empty: a lone \r in a 1-byte chunk ends an
                # EMPTY line, which still has to be counted.)
                parts = settled.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n")
                if ends_with_break:
                    parts.pop()  # the empty tail after the final break
                for line_bytes in parts:
                    handle_line(line_bytes)
                    if stop:
                        break
        # had_more is set by the line-level logic exactly when a line exists
        # past the returned window (the limit was reached by another line,
        # or the byte cap cut the window), matching the local reader; a file
        # that ends exactly at the window's end leaves it False.
        return kept, cut, had_more, line_cut


__all__ = ["SandboxRead"]
