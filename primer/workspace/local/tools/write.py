"""``write`` -- create or replace a file inside the workspace."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from primer.model.except_ import BadRequestError, ConflictError
from primer.workspace.tool import ToolCallContext, ToolResult, WorkspaceTool
from primer.workspace.local.tools._common import resolve_workspace_path


class WriteArgs(BaseModel):
    """Arguments for the ``write`` tool."""

    path: str = Field(..., description="File path relative to the workspace root.")
    content: str = Field(..., description="Full file contents.")
    mode: str | None = Field(
        default=None,
        description="Octal mode string (e.g. '0755'). Defaults to 0644 on most backends.",
    )
    force: bool = Field(
        default=False,
        description=(
            "If True, skip the read-before-write check. Use sparingly: "
            "the rule exists to prevent the agent from clobbering files "
            "it hasn't seen."
        ),
    )


class Write(WorkspaceTool):
    """Create or replace a file.

    Refuses to overwrite an existing file that the agent hasn't read in
    the current session unless ``force=True``. New files (target
    doesn't exist) are always allowed. Parent directories are created
    on demand.

    Output: ``wrote <bytes> bytes to <path>``.
    """

    id: ClassVar[str] = "write"
    description: ClassVar[str] = (
        "Create or replace a file. Refuses to overwrite a file you "
        "haven't read this session unless force=True is set."
    )

    def __init__(self, workspace_root: Path) -> None:
        self._root = Path(workspace_root)

    def parameters(self) -> type[BaseModel]:
        return WriteArgs

    async def execute(self, args: BaseModel, ctx: ToolCallContext) -> ToolResult:
        assert isinstance(args, WriteArgs)
        target = resolve_workspace_path(self._root, args.path)

        # Read-before-write rule: only enforced when overwriting an
        # existing file. New files are always allowed.
        if target.exists():
            if not args.force and not ctx.session.was_read(args.path):
                raise ConflictError(
                    f"refusing to overwrite {args.path!r}: read it first "
                    "or pass force=True"
                )

        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_text, args.content, encoding="utf-8")
        if args.mode is not None:
            try:
                octal = int(args.mode, 8)
            except ValueError as exc:
                raise BadRequestError(f"mode must be octal, got {args.mode!r}") from exc
            try:
                await asyncio.to_thread(target.chmod, octal)
            except (OSError, NotImplementedError):
                # POSIX modes are not always meaningful (Windows, etc.);
                # fail soft.
                pass

        # Mark as read so a later overwrite of OUR content doesn't trip
        # the read-before-write rule.
        ctx.session.mark_read(args.path)
        size = len(args.content.encode("utf-8"))
        return ToolResult(output=f"wrote {size} bytes to {args.path}")


__all__ = ["Write", "WriteArgs"]
