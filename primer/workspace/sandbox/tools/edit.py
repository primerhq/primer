"""``edit`` for the sandbox backend."""

from __future__ import annotations

import difflib
from typing import ClassVar

from pydantic import BaseModel

from primer.int.sandbox import Sandbox
from primer.model.chat import ToolExample
from primer.model.except_ import BadRequestError, NotFoundError
from primer.workspace.local.tools.edit import EditArgs
from primer.workspace.runtime.protocol import ErrorCode
from primer.workspace.runtime.runtime_client import RuntimeError as RuntimeOpError
from primer.workspace.sandbox.tools._common import resolve_sandbox_path
from primer.workspace.tool import ToolCallContext, ToolResult, WorkspaceTool


class SandboxEdit(WorkspaceTool):
    """String-replace edit dispatched through Sandbox file ops."""

    id: ClassVar[str] = "edit"
    interruptible: ClassVar[bool] = False   # a cancelled await cannot undo a write already sent: it would only drop the real result
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
        if not args.old_string:   # "" matches between every character
            raise BadRequestError("old_string must not be empty")
        target = resolve_sandbox_path(self._root, args.path)
        info = await self._sandbox.stat(target)
        if info is None:
            raise NotFoundError(f"{args.path!r} not found")
        if info.kind != "file":
            raise BadRequestError(f"{args.path!r} is not a file")

        # surrogateescape round-trips bytes that are not UTF-8, so the write changes only the replaced text.
        original = (await self._sandbox.read_file(target)).decode(
            "utf-8", errors="surrogateescape",
        )
        count = original.count(args.old_string)
        if count == 0:
            raise BadRequestError(f"old_string not found in {args.path!r}")
        if count > 1 and not args.replace_all:
            raise BadRequestError(
                f"old_string is non-unique in {args.path!r} "
                f"({count} occurrences); pass replace_all=true"
            )
        updated = self._apply(original, args)

        cas = getattr(self._sandbox, "write_file_cas", None)
        if cas is None:
            # Legacy Sandbox impl without CAS: the old read-modify-write.
            await self._sandbox.write_file(
                target, updated.encode("utf-8", errors="surrogateescape")
            )
        else:
            # Compare-and-swap (protocol 1.8): the write lands only while the
            # file still holds the bytes we read, so a concurrent edit fails
            # with a clear error instead of silently clobbering ours (review
            # 2026-11-23, 5.9). On a mismatch, retry ONCE against a fresh
            # read; a second mismatch is a clear failure.
            try:
                applied = await cas(
                    target,
                    original.encode("utf-8", errors="surrogateescape"),
                    updated.encode("utf-8", errors="surrogateescape"),
                )
            except RuntimeOpError as exc:
                # A runtime below protocol 1.8 answers the op EUNSUPPORTED:
                # the legacy read-modify-write, as on a Sandbox without the
                # method (a WSSandbox over an older image has it).
                if exc.code != ErrorCode.EUNSUPPORTED:
                    raise
                await self._sandbox.write_file(
                    target, updated.encode("utf-8", errors="surrogateescape")
                )
            else:
                if not applied:
                    fresh = (await self._sandbox.read_file(target)).decode(
                        "utf-8", errors="surrogateescape",
                    )
                    fresh_count = fresh.count(args.old_string)
                    if fresh_count == 0 or (fresh_count > 1 and not args.replace_all):
                        raise BadRequestError(
                            "file changed during edit; re-read and retry"
                        )
                    original = fresh
                    updated = self._apply(original, args)
                    applied = await cas(
                        target,
                        original.encode("utf-8", errors="surrogateescape"),
                        updated.encode("utf-8", errors="surrogateescape"),
                    )
                    if not applied:
                        raise BadRequestError(
                            "file changed during edit; re-read and retry"
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
        # The diff shows an undecodable byte as U+FFFD, as it did before the round-trip.
        output = "\n".join(diff_lines).encode("utf-8", errors="surrogateescape").decode("utf-8", errors="replace")
        return ToolResult(output=output)

    @staticmethod
    def _apply(original: str, args: EditArgs) -> str:
        """The replacement applied to *original* (first occurrence, or all)."""
        if args.replace_all:
            return original.replace(args.old_string, args.new_string)
        return original.replace(args.old_string, args.new_string, 1)


__all__ = ["SandboxEdit"]
