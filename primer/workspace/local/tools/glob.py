"""``glob`` -- find files by glob pattern, sorted by modification time."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from primer.model.chat import ToolExample
from primer.model.except_ import BadRequestError, NotFoundError
from primer.workspace.tool import ToolCallContext, ToolResult, WorkspaceTool
from primer.workspace.local.tools._common import resolve_workspace_path, workspace_relative


class GlobArgs(BaseModel):
    """Arguments for the ``glob`` tool."""

    pattern: str = Field(
        ...,
        min_length=1,
        description="Glob pattern (e.g. 'src/**/*.py').",
    )
    path: str = Field(
        default=".",
        description="Root for the search (relative to the workspace root).",
    )
    limit: int | None = Field(
        default=250,
        ge=1,
        description="Max number of paths to return. Pass null for no cap.",
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="Number of leading matches to skip (after sorting).",
    )


class Glob(WorkspaceTool):
    """Find files by glob pattern.

    Output: one matching path per line (relative to the workspace
    root), sorted by modification time newest-first. Pagination via
    ``offset`` + ``limit``.

    Sets :attr:`ToolResult.truncated` to True when more matches exist
    beyond the returned window so the runtime skips outer truncation.
    """

    id: ClassVar[str] = "glob"
    description: ClassVar[str] = (
        "Find files by glob pattern (e.g. 'src/**/*.py'). Results "
        "sorted newest-first; paginate with offset+limit.\n\n"
        "Use when finding files by name pattern; not for searching file "
        "contents (use ``grep``)."
    )
    examples: ClassVar[list[ToolExample]] = [
        ToolExample(
            args={"pattern": "**/*.py", "path": "."},
            returns="python files newest-first",
        ),
        ToolExample(args={"pattern": "*.ts", "path": "src"}),
    ]

    def __init__(self, workspace_root: Path) -> None:
        self._root = Path(workspace_root)

    def parameters(self) -> type[BaseModel]:
        return GlobArgs

    async def execute(self, args: BaseModel, ctx: ToolCallContext) -> ToolResult:
        del ctx
        assert isinstance(args, GlobArgs)
        root = resolve_workspace_path(self._root, args.path)
        if not root.exists():
            raise NotFoundError(f"{args.path!r} not found")
        if not root.is_dir():
            raise BadRequestError(f"{args.path!r} is not a directory")

        matches = await asyncio.to_thread(_glob_with_mtime, root, args.pattern)
        # Newest first; ties broken alphabetically for determinism.
        matches.sort(key=lambda t: (-t[0], t[1].as_posix()))

        end = (
            args.offset + args.limit
            if args.limit is not None
            else len(matches)
        )
        page = matches[args.offset : end]
        rels = [workspace_relative(self._root, p) for _, p in page]

        truncated = args.offset > 0 or end < len(matches)
        return ToolResult(output="\n".join(rels), truncated=truncated)


def _glob_with_mtime(root: Path, pattern: str) -> list[tuple[float, Path]]:
    out: list[tuple[float, Path]] = []
    for p in root.glob(pattern):
        try:
            out.append((p.stat().st_mtime, p))
        except OSError:
            # Symlink to nowhere, race with concurrent delete, etc.
            continue
    return out


__all__ = ["Glob", "GlobArgs"]
