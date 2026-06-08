"""``grep`` -- regex search across workspace files."""

from __future__ import annotations

import asyncio
import fnmatch
import re
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from primer.model.chat import ToolExample
from primer.model.except_ import BadRequestError, NotFoundError
from primer.workspace.tool import ToolCallContext, ToolResult, WorkspaceTool
from primer.workspace.local.tools._common import resolve_workspace_path, workspace_relative


_BINARY_SNIFF_BYTES = 1024
_DEFAULT_HEAD_LIMIT = 250


class GrepArgs(BaseModel):
    """Arguments for the ``grep`` tool."""

    pattern: str = Field(..., min_length=1, description="Regular expression.")
    path: str = Field(
        default=".",
        description="File or directory to search (relative to the workspace root).",
    )
    glob: str | None = Field(
        default=None,
        description="Filter files by glob (e.g. '*.py').",
    )
    output_mode: Literal["content", "files_with_matches", "count"] = Field(
        default="files_with_matches",
        description=(
            "'content' = <path>:<lineno>:<text>; "
            "'files_with_matches' = one path per line; "
            "'count' = <path>:<n>."
        ),
    )
    case_insensitive: bool = Field(default=False)
    multiline: bool = Field(
        default=False,
        description="Allow . to match newlines (re.DOTALL) and ^/$ to match each line (re.MULTILINE).",
    )
    context: int = Field(
        default=0,
        ge=0,
        description="Lines of context around each match (only honoured by 'content' mode).",
    )
    head_limit: int | None = Field(
        default=_DEFAULT_HEAD_LIMIT,
        ge=1,
        description="Cap on output lines. Pass null for no cap.",
    )


class Grep(WorkspaceTool):
    """Find content in workspace files.

    Pure-Python implementation: walks files under ``path`` (or treats
    ``path`` as a single file), applies the optional file ``glob``
    filter, and matches each line (or each whole-file blob in
    multiline mode) against the supplied regex. Output shape depends
    on ``output_mode``.

    Skips binary files (NUL-byte sniff) silently.

    Sets :attr:`ToolResult.truncated` to True when the head_limit cap
    fires so the runtime skips outer truncation.
    """

    id: ClassVar[str] = "grep"
    description: ClassVar[str] = (
        "Search file contents with a regex. Choose output_mode "
        "('files_with_matches', 'content', or 'count') and optionally "
        "filter files by glob.\n\n"
        "Use when searching file contents by regex; not for finding "
        "files by name (use ``glob``)."
    )
    examples: ClassVar[list[ToolExample]] = [
        ToolExample(
            args={"pattern": "TODO", "path": "."},
            returns="files containing TODO",
        ),
        ToolExample(
            args={
                "pattern": "def .*",
                "output_mode": "content",
                "glob": "*.py",
            },
            returns="matching lines",
        ),
    ]

    def __init__(self, workspace_root: Path) -> None:
        self._root = Path(workspace_root)

    def parameters(self) -> type[BaseModel]:
        return GrepArgs

    async def execute(self, args: BaseModel, ctx: ToolCallContext) -> ToolResult:
        del ctx
        assert isinstance(args, GrepArgs)
        target = resolve_workspace_path(self._root, args.path)
        if not target.exists():
            raise NotFoundError(f"{args.path!r} not found")

        try:
            flags = 0
            if args.case_insensitive:
                flags |= re.IGNORECASE
            if args.multiline:
                flags |= re.DOTALL | re.MULTILINE
            regex = re.compile(args.pattern, flags)
        except re.error as exc:
            raise BadRequestError(f"invalid regex: {exc}") from exc

        files = await asyncio.to_thread(_collect_files, target, args.glob)
        lines, full_count = await asyncio.to_thread(
            _grep_files,
            files,
            regex=regex,
            mode=args.output_mode,
            context=args.context,
            multiline=args.multiline,
            workspace_root=self._root,
        )

        capped: list[str]
        truncated = False
        if args.head_limit is not None and len(lines) > args.head_limit:
            capped = lines[: args.head_limit]
            truncated = True
        else:
            capped = lines

        del full_count  # reserved for future "summary" output enhancement
        return ToolResult(output="\n".join(capped), truncated=truncated)


def _collect_files(target: Path, glob_filter: str | None) -> list[Path]:
    if target.is_file():
        return [target]
    out: list[Path] = []
    for p in target.rglob("*"):
        if not p.is_file():
            continue
        if glob_filter is not None and not fnmatch.fnmatch(p.name, glob_filter):
            continue
        out.append(p)
    out.sort(key=lambda p: p.as_posix())
    return out


def _grep_files(
    files: list[Path],
    *,
    regex: re.Pattern[str],
    mode: str,
    context: int,
    multiline: bool,
    workspace_root: Path,
) -> tuple[list[str], int]:
    """Return (lines, total-match-count) for the requested mode."""
    out: list[str] = []
    total = 0
    for path in files:
        try:
            with path.open("rb") as fh:
                head = fh.read(_BINARY_SNIFF_BYTES)
        except OSError:
            continue
        if b"\x00" in head:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = workspace_relative(workspace_root, path)

        if multiline:
            matches_ml = list(regex.finditer(text))
            if not matches_ml:
                continue
            total += len(matches_ml)
            if mode == "files_with_matches":
                out.append(rel)
                continue
            if mode == "count":
                out.append(f"{rel}:{len(matches_ml)}")
                continue
            for m in matches_ml:
                snippet = m.group(0).replace("\n", "\\n")
                out.append(f"{rel}::{snippet}")
            continue

        line_matches: list[tuple[int, str]] = []
        for lineno, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                line_matches.append((lineno, line))

        if not line_matches:
            continue
        total += len(line_matches)

        if mode == "files_with_matches":
            out.append(rel)
            continue
        if mode == "count":
            out.append(f"{rel}:{len(line_matches)}")
            continue
        # content mode
        all_lines = text.splitlines()
        emitted: set[int] = set()
        for lineno, _line in line_matches:
            start = max(0, lineno - 1 - context)
            end = min(len(all_lines), lineno + context)
            for i in range(start, end):
                if i in emitted:
                    continue
                emitted.add(i)
                out.append(f"{rel}:{i + 1}:{all_lines[i]}")
    return out, total


__all__ = ["Grep", "GrepArgs"]
