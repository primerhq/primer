"""``edit`` -- string-replace edit producing a unified diff."""

from __future__ import annotations

import asyncio
import difflib
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from matrix.model.except_ import BadRequestError, NotFoundError
from matrix.workspace.tool import ToolCallContext, ToolResult, WorkspaceTool
from matrix.workspace.tools._common import resolve_workspace_path


class EditArgs(BaseModel):
    """Arguments for the ``edit`` tool."""

    path: str = Field(..., description="File path relative to the workspace root.")
    old_string: str = Field(..., description="Exact substring to replace.")
    new_string: str = Field(..., description="Replacement.")
    replace_all: bool = Field(
        default=False,
        description=(
            "Replace every occurrence. When False (the default), "
            "old_string MUST be unique in the file."
        ),
    )


class Edit(WorkspaceTool):
    """Workhorse string-replace edit.

    Errors clearly when ``old_string`` is not found, or is non-unique
    without ``replace_all``. Output is a unified diff of the change
    (no leading/trailing line terminators).
    """

    id: ClassVar[str] = "edit"
    description: ClassVar[str] = (
        "Replace a substring in a file. By default old_string must be "
        "unique; pass replace_all=true to replace every occurrence."
    )

    def __init__(self, workspace_root: Path) -> None:
        self._root = Path(workspace_root)

    def parameters(self) -> type[BaseModel]:
        return EditArgs

    async def execute(self, args: BaseModel, ctx: ToolCallContext) -> ToolResult:
        del ctx
        assert isinstance(args, EditArgs)
        if args.old_string == args.new_string:
            raise BadRequestError("old_string and new_string are identical")
        target = resolve_workspace_path(self._root, args.path)
        if not target.exists():
            raise NotFoundError(f"{args.path!r} not found")
        if not target.is_file():
            raise BadRequestError(f"{args.path!r} is not a file")

        original = await asyncio.to_thread(
            target.read_text, encoding="utf-8"
        )
        count = original.count(args.old_string)
        if count == 0:
            raise BadRequestError(
                f"old_string not found in {args.path!r}"
            )
        if count > 1 and not args.replace_all:
            raise BadRequestError(
                f"old_string is non-unique in {args.path!r} "
                f"({count} occurrences); pass replace_all=true"
            )

        if args.replace_all:
            updated = original.replace(args.old_string, args.new_string)
        else:
            updated = original.replace(args.old_string, args.new_string, 1)

        await asyncio.to_thread(
            target.write_text, updated, encoding="utf-8"
        )

        diff_lines = list(
            difflib.unified_diff(
                original.splitlines(),
                updated.splitlines(),
                fromfile=args.path,
                tofile=args.path,
                lineterm="",
            )
        )
        return ToolResult(output="\n".join(diff_lines))


__all__ = ["Edit", "EditArgs"]
