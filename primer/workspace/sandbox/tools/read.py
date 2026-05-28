"""``read`` for the sandbox backend."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

from primer.int.sandbox import Sandbox
from primer.model.except_ import BadRequestError, NotFoundError
from primer.workspace.local.tools.read import ReadArgs
from primer.workspace.sandbox.tools._common import resolve_sandbox_path
from primer.workspace.tool import ToolCallContext, ToolResult, WorkspaceTool


_BINARY_SNIFF_BYTES = 1024


class SandboxRead(WorkspaceTool):
    """Read a file with offset+limit pagination via Sandbox."""

    id: ClassVar[str] = "read"
    description: ClassVar[str] = (
        "Read a file with offset+limit pagination. Output prefixes "
        "lines with their numbers. Binary files return a summary."
    )

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
            f"{args.offset + i + 1:>6}→{line}"
            for i, line in enumerate(sliced)
        )
        ctx.session.mark_read(args.path)
        truncated = args.offset > 0 or end < len(lines)
        return ToolResult(output=formatted, truncated=truncated)


__all__ = ["SandboxRead"]
