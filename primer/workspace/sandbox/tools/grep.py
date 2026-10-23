"""``grep`` for the sandbox backend.

Walks the sandbox directory tree via :class:`Sandbox.list_dir`, reads
files via :class:`Sandbox.read_file`, applies the regex in Python.
Slower than ``rg`` shelled into the container but fully portable.

The pattern is bounded exactly as on the local backend (G6-4): the same
compile, the same per-call budget, and each file is matched in a worker
thread with the GIL released, never inside the coroutine.
"""

from __future__ import annotations

import asyncio
import fnmatch
from typing import ClassVar

from pydantic import BaseModel

from primer.int.sandbox import Sandbox
from primer.model.chat import ToolExample
from primer.model.except_ import NotFoundError
from primer.workspace.local.tools.grep import (
    GrepArgs,
    compile_grep_pattern,
    grep_budget,
    grep_budget_error,
    grep_text,
)
from primer.workspace.sandbox.tools._common import (
    resolve_sandbox_path,
    workspace_relative,
)
from primer.workspace.tool import ToolCallContext, ToolResult, WorkspaceTool


_BINARY_SNIFF_BYTES = 1024


class SandboxGrep(WorkspaceTool):
    """Regex search across sandbox files."""

    id: ClassVar[str] = "grep"
    description: ClassVar[str] = (
        "Search file contents with a regex. Choose output_mode "
        "('files_with_matches', 'content', or 'count') and optionally "
        "filter files by glob.\n\n"
        "Use when searching file contents by regex; not for finding "
        "files by name (use ``glob``)."
    )
    examples: ClassVar[list[ToolExample]] = [
        ToolExample(
            args={"pattern": "TODO", "path": "."},
            returns="files containing TODO",
        ),
        ToolExample(
            args={
                "pattern": "def .*",
                "output_mode": "content",
                "glob": "*.py",
            },
            returns="matching lines",
        ),
    ]

    def __init__(self, sandbox: Sandbox, *, workspace_root: str) -> None:
        self._sandbox = sandbox
        self._root = workspace_root

    def parameters(self) -> type[BaseModel]:
        return GrepArgs

    async def execute(
        self, args: BaseModel, ctx: ToolCallContext,
    ) -> ToolResult:
        del ctx
        assert isinstance(args, GrepArgs)
        target_abs = resolve_sandbox_path(self._root, args.path)
        info = await self._sandbox.stat(target_abs)
        if info is None:
            raise NotFoundError(f"{args.path!r} not found")

        rx = compile_grep_pattern(args)

        files: list[str] = []
        if info.kind == "file":
            files.append(target_abs)
        else:
            await self._collect_files(target_abs, args.glob, files)

        out_lines: list[str] = []
        match_count = 0
        file_count = 0
        budget = grep_budget()
        for path_abs in files:
            body = await self._sandbox.read_file(path_abs)
            if b"\x00" in body[:_BINARY_SNIFF_BYTES]:
                continue
            text = body.decode("utf-8", errors="replace")
            rel = workspace_relative(self._root, path_abs)
            try:
                lines, count = await asyncio.to_thread(
                    grep_text,
                    text,
                    rel,
                    rx=rx,
                    mode=args.output_mode,
                    context=args.context,
                    multiline=args.multiline,
                    budget=budget,
                )
            except TimeoutError as exc:
                raise grep_budget_error(budget) from exc
            if count:
                match_count += count
                file_count += 1
                out_lines.extend(lines)

        truncated = False
        if args.head_limit is not None and len(out_lines) > args.head_limit:
            out_lines = out_lines[:args.head_limit]
            truncated = True
        # UX reconcile wave 5: same match_count/file_count/truncated
        # metadata as the local backend's grep.py, so a "searched N
        # files" chip reads identically regardless of which workspace
        # backend ran the call.
        return ToolResult(
            output="\n".join(out_lines),
            truncated=truncated,
            metadata={
                "match_count": match_count,
                "file_count": file_count,
                "truncated": truncated,
            },
        )

    async def _collect_files(
        self,
        dir_abs: str,
        glob_filter: str | None,
        out: list[str],
    ) -> None:
        for entry in await self._sandbox.list_dir(dir_abs):
            child_abs = f"{dir_abs}/{entry.path}"
            if entry.kind == "dir":
                await self._collect_files(child_abs, glob_filter, out)
                continue
            if entry.kind != "file":
                continue
            if glob_filter is not None and not fnmatch.fnmatch(
                entry.path, glob_filter,
            ):
                continue
            out.append(child_abs)


__all__ = ["SandboxGrep"]
