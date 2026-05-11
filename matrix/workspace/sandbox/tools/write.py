"""``write`` for the sandbox backend."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

from matrix.int.sandbox import Sandbox
from matrix.model.except_ import BadRequestError, ConflictError
from matrix.workspace.local.tools.write import WriteArgs
from matrix.workspace.sandbox.tools._common import resolve_sandbox_path
from matrix.workspace.tool import ToolCallContext, ToolResult, WorkspaceTool


class SandboxWrite(WorkspaceTool):
    """Create or replace a file via Sandbox (read-before-write rule)."""

    id: ClassVar[str] = "write"
    description: ClassVar[str] = (
        "Create or replace a file. Refuses to overwrite a file you "
        "haven't read this session unless force=True is set."
    )

    def __init__(self, sandbox: Sandbox, *, workspace_root: str) -> None:
        self._sandbox = sandbox
        self._root = workspace_root

    def parameters(self) -> type[BaseModel]:
        return WriteArgs

    async def execute(
        self, args: BaseModel, ctx: ToolCallContext,
    ) -> ToolResult:
        assert isinstance(args, WriteArgs)
        target = resolve_sandbox_path(self._root, args.path)

        existing = await self._sandbox.stat(target)
        if existing is not None:
            if existing.kind == "dir":
                raise BadRequestError(
                    f"{args.path!r} is a directory; cannot overwrite "
                    "with file content"
                )
            if not args.force and not ctx.session.was_read(args.path):
                raise ConflictError(
                    f"refusing to overwrite {args.path!r}: read it first "
                    "or pass force=True"
                )

        mode_int: int | None = None
        if args.mode is not None:
            try:
                mode_int = int(args.mode, 8)
            except ValueError as exc:
                raise BadRequestError(
                    f"mode must be octal, got {args.mode!r}"
                ) from exc

        await self._sandbox.write_file(
            target, args.content.encode("utf-8"), mode=mode_int,
        )
        ctx.session.mark_read(args.path)
        size = len(args.content.encode("utf-8"))
        return ToolResult(output=f"wrote {size} bytes to {args.path}")


__all__ = ["SandboxWrite"]
