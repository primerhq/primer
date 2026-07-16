"""``edit`` -- string-replace edit producing a unified diff."""

from __future__ import annotations

import asyncio
import contextlib
import difflib
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from primer.model.chat import ToolExample
from primer.model.except_ import BadRequestError, NotFoundError
from primer.workspace._locks import WorkspaceLockTable
from primer.workspace.tool import ToolCallContext, ToolResult, WorkspaceTool
from primer.workspace.local.tools._common import resolve_workspace_path


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
        "unique; pass replace_all=true to replace every occurrence.\n\n"
        "Use when replacing a substring in a file; old_string must be "
        "unique unless replace_all is set."
    )
    examples: ClassVar[list[ToolExample]] = [
        ToolExample(
            args={"path": "a.py", "old_string": "foo", "new_string": "bar"},
            returns="one replacement",
        ),
        ToolExample(
            args={
                "path": "a.py",
                "old_string": "x",
                "new_string": "y",
                "replace_all": True,
            },
            returns="all occurrences",
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
        the whole read-modify-write runs under the Tier-A write lock and the
        final write is atomic (temp file + os.replace), so a concurrent
        reader never sees a torn buffer and a same-dir writer / exec
        serializes. ``strict`` collapses the scope key to the workspace root.
        """
        self._root = Path(workspace_root)
        self._locks = locks
        self._strict = strict

    def parameters(self) -> type[BaseModel]:
        return EditArgs

    def _scope_key(self, target: Path) -> str:
        """Tier-A scope key -- MUST match the write / exec tool derivation."""
        if self._strict:
            return str(self._root.resolve())
        return str(target.parent)

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

        # Reuse the backend's atomic write primitive; imported lazily to
        # avoid a circular import (workspace.py imports this module).
        from primer.workspace.local.workspace import _atomic_write_bytes

        if self._locks is not None:
            lock_ctx = self._locks.hold_write(self._scope_key(target), str(target))
        else:
            lock_ctx = contextlib.nullcontext()
        # Hold the lock across the ENTIRE read-modify-write so the file the
        # diff is computed from is the same one written back.
        async with lock_ctx:
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
                _atomic_write_bytes, target, updated.encode("utf-8")
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
