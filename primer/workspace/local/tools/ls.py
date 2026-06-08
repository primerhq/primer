"""``ls`` -- list directory contents inside a workspace."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from primer.model.chat import ToolExample
from primer.model.except_ import BadRequestError, NotFoundError
from primer.workspace.tool import ToolCallContext, ToolResult, WorkspaceTool
from primer.workspace.local.tools._common import resolve_workspace_path


class LsArgs(BaseModel):
    """Arguments for the ``ls`` tool."""

    path: str = Field(
        default=".",
        description="Directory path relative to the workspace root.",
    )
    show_hidden: bool = Field(
        default=False,
        description="Include dotfiles in the listing.",
    )
    recursive: bool = Field(
        default=False,
        description="Walk subdirectories.",
    )
    max_depth: int | None = Field(
        default=None,
        description="Maximum recursion depth when ``recursive`` is True.",
        ge=1,
    )


class Ls(WorkspaceTool):
    """List entries in a workspace directory.

    Output: one line per entry, ``<type> <size> <name>`` where type is
    ``f`` / ``d`` / ``l`` (file / dir / symlink). Sorted alphabetically.
    Sizes are in bytes; directories and symlinks report 0.
    """

    id: ClassVar[str] = "ls"
    description: ClassVar[str] = (
        "List the contents of a directory. Returns one entry per line "
        "with kind, size, mtime, and name.\n\n"
        "Use when you need a directory listing; not for file contents "
        "(use ``read``)."
    )
    examples: ClassVar[list[ToolExample]] = [
        ToolExample(args={"path": "src"}, returns="entries in src"),
        ToolExample(
            args={"path": ".", "recursive": True},
            returns="recursive listing",
        ),
    ]

    def __init__(self, workspace_root: Path) -> None:
        self._root = Path(workspace_root)

    def parameters(self) -> type[BaseModel]:
        return LsArgs

    async def execute(self, args: BaseModel, ctx: ToolCallContext) -> ToolResult:
        del ctx
        assert isinstance(args, LsArgs)
        target = resolve_workspace_path(self._root, args.path)
        if not target.exists():
            raise NotFoundError(f"{args.path!r} not found")
        if not target.is_dir():
            raise BadRequestError(f"{args.path!r} is not a directory")

        entries = await asyncio.to_thread(
            _walk,
            target,
            show_hidden=args.show_hidden,
            recursive=args.recursive,
            max_depth=args.max_depth,
        )
        lines = [_format_entry(e, target) for e in entries]
        return ToolResult(output="\n".join(lines))


def _walk(
    root: Path,
    *,
    show_hidden: bool,
    recursive: bool,
    max_depth: int | None,
) -> list[Path]:
    out: list[Path] = []

    def _visit(directory: Path, depth: int) -> None:
        try:
            children = sorted(directory.iterdir(), key=lambda p: p.name.lower())
        except PermissionError:
            return
        for child in children:
            if not show_hidden and child.name.startswith("."):
                continue
            out.append(child)
            if recursive and child.is_dir() and not child.is_symlink():
                if max_depth is None or depth + 1 < max_depth:
                    _visit(child, depth + 1)

    _visit(root, 0)
    return out


def _format_entry(entry: Path, base: Path) -> str:
    if entry.is_symlink():
        kind = "l"
        size = 0
    elif entry.is_dir():
        kind = "d"
        size = 0
    else:
        kind = "f"
        try:
            size = entry.stat().st_size
        except OSError:
            size = 0
    rel = entry.relative_to(base).as_posix()
    return f"{kind} {size:>10} {rel}"


__all__ = ["Ls", "LsArgs"]
