"""``write`` -- create or replace a file inside the workspace."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from primer.model.chat import ToolExample
from primer.model.except_ import BadRequestError, ConflictError
from primer.workspace._locks import WorkspaceLockTable
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
        "haven't read this session unless force=True is set.\n\n"
        "Use when creating or replacing a whole file; not for changing "
        "part of a file (use ``edit``)."
    )
    examples: ClassVar[list[ToolExample]] = [
        ToolExample(
            args={"path": "notes.txt", "content": "hello"},
            returns="file written",
        ),
        ToolExample(
            args={"path": "a.py", "content": "x = 1", "force": True},
            returns="overwrites unread file",
            note="force bypasses the read-before-write guard",
        ),
    ]

    def __init__(
        self,
        workspace_root: Path,
        *,
        locks: WorkspaceLockTable | None = None,
        strict: bool = False,
    ) -> None:
        """Construct.

        ``locks`` -- the shared per-workspace lock table. When supplied,
        this tool acquires the Tier-A write lock (scope THEN path) around
        the atomic write so concurrent writers / same-dir execs serialize.
        ``strict`` collapses the scope key to the workspace root instead of
        the file's parent dir (whole-root serialization).
        """
        self._root = Path(workspace_root)
        self._locks = locks
        self._strict = strict

    def parameters(self) -> type[BaseModel]:
        return WriteArgs

    def _scope_key(self, target: Path) -> str:
        """Tier-A scope key: the file's parent dir, or the root when strict.

        MUST match the exec tool's Tier-B derivation
        (``primer/workspace/local/tools/exec_.py``) and the workspace's
        ``_scope_key`` so a tool write and a same-dir exec share the same
        scope lock and therefore serialize.
        """
        if self._strict:
            return str(self._root.resolve())
        return str(target.parent)

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

        # Reuse the backend's atomic write primitive (temp file + os.replace)
        # so a concurrent reader never sees a torn / truncated file. Imported
        # lazily to avoid a circular import (workspace.py imports this module).
        from primer.workspace.local.workspace import _atomic_write_bytes

        content_bytes = args.content.encode("utf-8")
        if self._locks is not None:
            lock_ctx = self._locks.hold_write(self._scope_key(target), str(target))
        else:
            lock_ctx = contextlib.nullcontext()
        async with lock_ctx:
            await asyncio.to_thread(
                target.parent.mkdir, parents=True, exist_ok=True
            )
            await asyncio.to_thread(_atomic_write_bytes, target, content_bytes)
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
        size = len(content_bytes)
        return ToolResult(output=f"wrote {size} bytes to {args.path}")


__all__ = ["Write", "WriteArgs"]
