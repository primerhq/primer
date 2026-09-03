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


# 01a0645c: bounds a recursive=True walk's own entry count - max_depth
# alone does not (a wide-but-shallow tree, e.g. one huge flat directory,
# is uncapped by depth). Smaller than the paginated list_workspace_files
# tool's 10_000 (primer/toolset/workspaces.py) on purpose: this tool has
# no offset/limit at all, so the cap also bounds how much plain-text
# output lands in the model's context in one shot, not just walk cost.
_MAX_ENTRIES = 1_000


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
    # Kept byte-identical to primer.workspace.sandbox.tools.ls.SandboxLs's
    # description (drift guard: tests/toolset/test_local_tool_
    # descriptions.py) - an agent sees the same guidance for the "ls"
    # tool id regardless of workspace backend.
    description: ClassVar[str] = (
        "List the contents of a directory. Returns one entry per line "
        "with kind, size, mtime, and name. A recursive walk over a very "
        "large tree is capped; a trailing line notes it and the "
        "listing is then partial, not exhaustive.\n\n"
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

        # Over-fetch by one so a truncated walk is detected precisely
        # (len(entries) > _MAX_ENTRIES) rather than guessed from
        # len(entries) == _MAX_ENTRIES, which would also misfire when a
        # tree's true entry count happens to equal the cap exactly.
        entries = await asyncio.to_thread(
            _walk,
            target,
            show_hidden=args.show_hidden,
            recursive=args.recursive,
            max_depth=args.max_depth,
            max_entries=_MAX_ENTRIES + 1,
        )
        truncated = len(entries) > _MAX_ENTRIES
        if truncated:
            entries = entries[:_MAX_ENTRIES]
        lines = [_format_entry(e, target) for e in entries]
        if truncated:
            lines.append(
                f"... truncated at {_MAX_ENTRIES} entries (of unknown more)"
            )
        return ToolResult(output="\n".join(lines))


def _walk(
    root: Path,
    *,
    show_hidden: bool,
    recursive: bool,
    max_depth: int | None,
    max_entries: int,
) -> list[Path]:
    out: list[Path] = []

    def _visit(directory: Path, depth: int) -> bool:
        """Returns False once max_entries is reached, so a caller mid-
        sibling-loop or mid-recursion stops immediately - both
        appending AND descending into further subdirectories, not just
        the top-level walk."""
        try:
            children = sorted(directory.iterdir(), key=lambda p: p.name.lower())
        except PermissionError:
            return True
        for child in children:
            if not show_hidden and child.name.startswith("."):
                continue
            if len(out) >= max_entries:
                return False
            out.append(child)
            if recursive and child.is_dir() and not child.is_symlink():
                if max_depth is None or depth + 1 < max_depth:
                    if not _visit(child, depth + 1):
                        return False
        return True

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
