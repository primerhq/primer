"""``edit`` for the sandbox backend."""

from __future__ import annotations

import difflib
from typing import ClassVar

from pydantic import BaseModel

from matrix.int.sandbox import Sandbox
from matrix.model.except_ import BadRequestError, NotFoundError
from matrix.workspace.local.tools.edit import EditArgs
from matrix.workspace.sandbox.tools._common import resolve_sandbox_path
from matrix.workspace.tool import ToolCallContext, ToolResult, WorkspaceTool


class SandboxEdit(WorkspaceTool):
    """String-replace edit dispatched through Sandbox file ops."""

    id: ClassVar[str] = "edit"
    description: ClassVar[str] = (
        "Replace a substring in a file. By default old_string must be "
        "unique; pass replace_all=true to replace every occurrence."
    )

    def __init__(self, sandbox: Sandbox, *, workspace_root: str) -> None:
        self._sandbox = sandbox
        self._root = workspace_root

    def parameters(self) -> type[BaseModel]:
        return EditArgs

    async def execute(
        self, args: BaseModel, ctx: ToolCallContext,
    ) -> ToolResult:
        del ctx
        assert isinstance(args, EditArgs)
        if args.old_string == args.new_string:
            raise BadRequestError("old_string and new_string are identical")
        target = resolve_sandbox_path(self._root, args.path)
        info = await self._sandbox.stat(target)
        if info is None:
            raise NotFoundError(f"{args.path!r} not found")
        if info.kind != "file":
            raise BadRequestError(f"{args.path!r} is not a file")

        original = (await self._sandbox.read_file(target)).decode(
            "utf-8", errors="replace",
        )
        count = original.count(args.old_string)
        if count == 0:
            raise BadRequestError(f"old_string not found in {args.path!r}")
        if count > 1 and not args.replace_all:
            raise BadRequestError(
                f"old_string is non-unique in {args.path!r} "
                f"({count} occurrences); pass replace_all=true"
            )

        if args.replace_all:
            updated = original.replace(args.old_string, args.new_string)
        else:
            updated = original.replace(args.old_string, args.new_string, 1)

        await self._sandbox.write_file(target, updated.encode("utf-8"))

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


__all__ = ["SandboxEdit"]
