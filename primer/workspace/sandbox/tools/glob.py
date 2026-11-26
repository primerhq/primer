"""``glob`` for the sandbox backend.

Walks the sandbox in ONE recursive, capped ``walk`` op (protocol 1.8) and
matches with :mod:`fnmatch`, instead of one ``list_dir`` round-trip per
directory (review 2026-11-23, 5.11). A sandbox without ``walk`` (an older
impl) falls back to the legacy per-directory walk. Slower than ``find``
shelled into the container, but portable across any Sandbox impl.
"""

from __future__ import annotations

import fnmatch
from typing import ClassVar

from pydantic import BaseModel

from primer.int.sandbox import Sandbox
from primer.model.chat import ToolExample
from primer.model.except_ import BadRequestError, NotFoundError
from primer.workspace.local.tools.glob import GlobArgs
from primer.workspace.runtime.protocol import ErrorCode
from primer.workspace.runtime.runtime_client import RuntimeError as RuntimeOpError
from primer.workspace.sandbox.tools._common import (
    resolve_sandbox_path,
    workspace_relative,
)
from primer.workspace.tool import ToolCallContext, ToolResult, WorkspaceTool


class SandboxGlob(WorkspaceTool):
    """Find files by glob pattern via Sandbox traversal."""

    read_only: ClassVar[bool] = True

    id: ClassVar[str] = "glob"
    description: ClassVar[str] = (
        "Find files by glob pattern (e.g. 'src/**/*.py'). Results "
        "sorted newest-first; paginate with offset+limit.\n\n"
        "Use when finding files by name pattern; not for searching file "
        "contents (use ``grep``)."
    )
    examples: ClassVar[list[ToolExample]] = [
        ToolExample(
            args={"pattern": "**/*.py", "path": "."},
            returns="python files newest-first",
        ),
        ToolExample(args={"pattern": "*.ts", "path": "src"}),
    ]

    def __init__(self, sandbox: Sandbox, *, workspace_root: str) -> None:
        self._sandbox = sandbox
        self._root = workspace_root

    def parameters(self) -> type[BaseModel]:
        return GlobArgs

    async def execute(
        self, args: BaseModel, ctx: ToolCallContext,
    ) -> ToolResult:
        del ctx
        assert isinstance(args, GlobArgs)
        root_abs = resolve_sandbox_path(self._root, args.path)
        info = await self._sandbox.stat(root_abs)
        if info is None:
            raise NotFoundError(f"{args.path!r} not found")
        if info.kind != "dir":
            raise BadRequestError(f"{args.path!r} is not a directory")

        walk = getattr(self._sandbox, "walk", None)
        entries: list[dict] | None = None
        walk_truncated = False
        if walk is not None:
            # One recursive walk in the sandbox (protocol 1.8) with an entry
            # and time cap, instead of one list_dir round-trip per directory
            # (review 2026-11-23, 5.11). A runtime that answers EUNSUPPORTED
            # (an older image) falls back to the legacy walk below.
            try:
                entries, walk_truncated = await walk(root_abs)
            except RuntimeOpError as exc:
                if exc.code != ErrorCode.EUNSUPPORTED:
                    raise
                entries = None

        matches: list[tuple[float, str]] = []
        if entries is not None:
            for e in entries:
                if e.get("is_dir"):
                    continue
                rel = e.get("path", "")
                if _matches(rel, args.pattern):
                    matches.append((float(e.get("mtime", 0.0)), f"{root_abs}/{rel}"))
        else:
            await self._collect(root_abs, args.pattern, matches, "")
        # Newest first; ties broken alphabetically for determinism.
        matches.sort(key=lambda t: (-t[0], t[1]))

        end = (
            args.offset + args.limit
            if args.limit is not None
            else len(matches)
        )
        page = matches[args.offset:end]
        rels = [
            workspace_relative(self._root, abs_path)
            for _, abs_path in page
        ]
        truncated = args.offset > 0 or end < len(matches) or walk_truncated
        return ToolResult(output="\n".join(rels), truncated=truncated)

    async def _collect(
        self,
        dir_abs: str,
        pattern: str,
        out: list[tuple[float, str]],
        rel_prefix: str,
    ) -> None:
        for entry in await self._sandbox.list_dir(dir_abs):
            child_abs = f"{dir_abs}/{entry.path}"
            child_rel = (
                f"{rel_prefix}/{entry.path}" if rel_prefix else entry.path
            )
            if entry.kind == "dir":
                await self._collect(child_abs, pattern, out, child_rel)
                continue
            if _matches(child_rel, pattern):
                ts = entry.modified_at.timestamp()
                out.append((ts, child_abs))


def _matches(rel: str, pattern: str) -> bool:
    """fnmatch is OK for ``*.py`` style; handle ``**`` by stripping it."""
    if "**" in pattern:
        # ``**/foo`` and ``foo/**`` → match the suffix anywhere.
        suffix = pattern.replace("**/", "").replace("/**", "")
        return fnmatch.fnmatchcase(rel, suffix) or fnmatch.fnmatchcase(
            rel.rsplit("/", 1)[-1], suffix
        )
    return fnmatch.fnmatchcase(rel, pattern) or fnmatch.fnmatchcase(
        rel.rsplit("/", 1)[-1], pattern
    )


__all__ = ["SandboxGlob"]
