"""``ls`` for the sandbox backend."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

from matrix.int.sandbox import Sandbox
from matrix.model.except_ import BadRequestError, NotFoundError
from matrix.workspace.local.tools.ls import LsArgs
from matrix.workspace.sandbox.tools._common import resolve_sandbox_path
from matrix.workspace.tool import ToolCallContext, ToolResult, WorkspaceTool


class SandboxLs(WorkspaceTool):
    """``ls``: list directory contents inside a sandbox."""

    id: ClassVar[str] = "ls"
    description: ClassVar[str] = (
        "List the contents of a directory. Returns one entry per line "
        "with kind, size, mtime, and name."
    )

    def __init__(self, sandbox: Sandbox, *, workspace_root: str) -> None:
        self._sandbox = sandbox
        self._root = workspace_root

    def parameters(self) -> type[BaseModel]:
        return LsArgs

    async def execute(
        self, args: BaseModel, ctx: ToolCallContext,
    ) -> ToolResult:
        del ctx
        assert isinstance(args, LsArgs)
        target = resolve_sandbox_path(self._root, args.path)
        info = await self._sandbox.stat(target)
        if info is None:
            raise NotFoundError(f"{args.path!r} not found")
        if info.kind != "dir":
            raise BadRequestError(f"{args.path!r} is not a directory")
        entries = await self._sandbox.list_dir(target)
        lines = []
        for fs in entries:
            mtime = fs.modified_at.strftime("%Y-%m-%dT%H:%M:%SZ")
            lines.append(
                f"{fs.kind:<7} {fs.size_bytes:>10} {mtime} {fs.path}"
            )
        return ToolResult(output="\n".join(lines))


__all__ = ["SandboxLs"]
