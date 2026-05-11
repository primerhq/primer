"""``grep`` for the sandbox backend.

Walks the sandbox directory tree via :class:`Sandbox.list_dir`, reads
files via :class:`Sandbox.read_file`, applies the regex in Python.
Slower than ``rg`` shelled into the container but fully portable.
"""

from __future__ import annotations

import fnmatch
import re
from typing import ClassVar

from pydantic import BaseModel

from matrix.int.sandbox import Sandbox
from matrix.model.except_ import BadRequestError, NotFoundError
from matrix.workspace.local.tools.grep import GrepArgs
from matrix.workspace.sandbox.tools._common import (
    resolve_sandbox_path,
    workspace_relative,
)
from matrix.workspace.tool import ToolCallContext, ToolResult, WorkspaceTool


_BINARY_SNIFF_BYTES = 1024


class SandboxGrep(WorkspaceTool):
    """Regex search across sandbox files."""

    id: ClassVar[str] = "grep"
    description: ClassVar[str] = (
        "Search file contents with a regex. Choose output_mode "
        "('files_with_matches', 'content', or 'count') and optionally "
        "filter files by glob."
    )

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

        try:
            flags = 0
            if args.case_insensitive:
                flags |= re.IGNORECASE
            if args.multiline:
                flags |= re.DOTALL | re.MULTILINE
            regex = re.compile(args.pattern, flags)
        except re.error as exc:
            raise BadRequestError(f"invalid regex: {exc}") from exc

        files: list[str] = []
        if info.kind == "file":
            files.append(target_abs)
        else:
            await self._collect_files(target_abs, args.glob, files)

        out_lines: list[str] = []
        for path_abs in files:
            body = await self._sandbox.read_file(path_abs)
            if b"\x00" in body[:_BINARY_SNIFF_BYTES]:
                continue
            text = body.decode("utf-8", errors="replace")
            rel = workspace_relative(self._root, path_abs)

            if args.multiline:
                matches = list(regex.finditer(text))
                if not matches:
                    continue
                if args.output_mode == "files_with_matches":
                    out_lines.append(rel)
                elif args.output_mode == "count":
                    out_lines.append(f"{rel}:{len(matches)}")
                else:  # content
                    for m in matches:
                        snippet = m.group(0).replace("\n", "\\n")
                        out_lines.append(f"{rel}::{snippet}")
                continue

            line_hits: list[tuple[int, str]] = []
            for lineno, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    line_hits.append((lineno, line))
            if not line_hits:
                continue
            if args.output_mode == "files_with_matches":
                out_lines.append(rel)
            elif args.output_mode == "count":
                out_lines.append(f"{rel}:{len(line_hits)}")
            else:  # content
                all_lines = text.splitlines()
                emitted: set[int] = set()
                for lineno, _line in line_hits:
                    start = max(0, lineno - 1 - args.context)
                    end = min(len(all_lines), lineno + args.context)
                    for i in range(start, end):
                        if i in emitted:
                            continue
                        emitted.add(i)
                        out_lines.append(f"{rel}:{i + 1}:{all_lines[i]}")

        truncated = False
        if args.head_limit is not None and len(out_lines) > args.head_limit:
            out_lines = out_lines[:args.head_limit]
            truncated = True
        return ToolResult(output="\n".join(out_lines), truncated=truncated)

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
