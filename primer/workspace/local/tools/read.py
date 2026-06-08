"""``read`` -- read a file with offset/limit pagination."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from primer.model.chat import ToolExample
from primer.model.except_ import BadRequestError, NotFoundError
from primer.workspace.tool import ToolCallContext, ToolResult, WorkspaceTool
from primer.workspace.local.tools._common import resolve_workspace_path


_BINARY_SNIFF_BYTES = 1024
_DEFAULT_LIMIT = 2000


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
    """

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

        text = await asyncio.to_thread(_read_text, target)
        lines = text.splitlines()
        end = (
            args.offset + args.limit
            if args.limit is not None
            else len(lines)
        )
        sliced = lines[args.offset : end]
        formatted = "\n".join(
            f"{args.offset + i + 1:>6}→{line}"
            for i, line in enumerate(sliced)
        )
        ctx.session.mark_read(args.path)

        truncated = args.offset > 0 or end < len(lines)
        return ToolResult(output=formatted, truncated=truncated)


def _read_head_bytes(path: Path, n: int) -> bytes:
    with path.open("rb") as fh:
        return fh.read(n)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _looks_binary(sample: bytes) -> bool:
    """Heuristic: NUL byte in the first chunk = binary."""
    return b"\x00" in sample


__all__ = ["Read", "ReadArgs"]
