"""``grep`` for the sandbox backend.

For a directory target the search runs as ONE recursive ``search`` op in the
sandbox (protocol 1.8): one round-trip instead of one walk plus one
``read_file`` per file, matched in the runtime with the ``regex`` library
under a per-line timeout, under a file cap, a per-file size cap, a match cap,
an output byte cap and a deadline (review 2026-11-23, 5.11). Unlike the local
grep these caps exist, so a result that hit one says so in its text (the
agent must know what was not searched). A single-file target, a sandbox
without ``search``, or a runtime that answers EUNSUPPORTED (older than 1.8)
falls back to the legacy per-file walk, which walks via
:class:`Sandbox.list_dir`, reads files via :class:`Sandbox.read_file` and
applies the bounded ``regex`` pattern in Python.

The pattern is bounded exactly as on the local backend (G6-4): the same
compile, the same per-call budget and per-search bound (the legacy path
matches each file on the user-regex bulk executor with the GIL released,
never inside the coroutine).
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

from primer.common.user_regex import UserRegexTimeout, run_bulk
from primer.int.sandbox import Sandbox
from primer.model.chat import ToolExample
from primer.model.except_ import NotFoundError
from primer.workspace.local.tools.grep import (
    GrepArgs,
    compile_grep_pattern,
    glob_matches,
    grep_budget,
    grep_budget_error,
    grep_text,
)
from primer.workspace.reserved import reserved_tree
from primer.workspace.runtime.protocol import ErrorCode
from primer.workspace.runtime.runtime_client import RuntimeError as RuntimeOpError
from primer.workspace.sandbox.tools._common import (
    resolve_sandbox_path,
    workspace_relative,
)
from primer.workspace.tool import ToolCallContext, ToolResult, WorkspaceTool


_BINARY_SNIFF_BYTES = 1024
# A runtime read refused for the entry itself (a symlinked directory the runtime lists as a file, a file gone since
# the walk): the entry is skipped. Any other runtime failure still fails the call.
_UNREADABLE = frozenset({ErrorCode.ENOENT, ErrorCode.EACCES, ErrorCode.EISDIR, ErrorCode.ENOTDIR})


# The caps the search runs under (sent explicitly so the notes below state exactly what applied).
_MAX_FILES = 2000
_MAX_FILE_BYTES = 1 << 20
_MAX_MATCHES = 5000
_OUTPUT_BYTES = 1 << 20
_DEADLINE_S = 30.0
_LINE_TIMEOUT_S = 2.0  # mirrors the runtime's per-line match bound


def _stop_notes(result: dict, glob: str | None = None) -> list[str]:
    """One ``[note: ...]`` line per cap the runtime's search hit: what was NOT searched or shown."""
    notes: list[str] = []
    for reason in result.get("stop_reasons") or ([result["stop_reason"]] if result.get("stop_reason") else []):
        if reason == "size_cap":
            skipped = int(result.get("skipped_large", 0))
            notes.append(
                f"{skipped} file(s) larger than {_MAX_FILE_BYTES} bytes were skipped and not searched"
            )
        elif reason == "file_cap":
            which = f"files matching {glob!r}" if glob else "files"
            notes.append(
                f"only the first {_MAX_FILES} {which} (breadth-first, shallow directories first) "
                "were searched; search a narrower path"
            )
        elif reason == "match_cap":
            notes.append(f"search stopped after {_MAX_MATCHES} matches; results are partial")
        elif reason == "time_cap":
            notes.append(f"search stopped at its {_DEADLINE_S:g} s deadline; results are partial")
        elif reason == "regex_timeout":
            notes.append(
                f"a line took longer than {_LINE_TIMEOUT_S:g} s to match (the pattern backtracks; "
                "simplify it); search stopped, results are partial"
            )
        elif reason == "output_cap":
            notes.append(f"output cut at {_OUTPUT_BYTES} bytes; narrow the search")
        else:
            notes.append(f"search stopped early ({reason}); results are partial")
    return notes


class SandboxGrep(WorkspaceTool):
    """Regex search across sandbox files."""

    read_only: ClassVar[bool] = True

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

    def __init__(
        self, sandbox: Sandbox, *, workspace_root: str,
        reserved_trees: tuple[str, ...] = (".state", ".tmp"),
    ) -> None:
        self._sandbox = sandbox
        self._root = workspace_root
        self._reserved = reserved_trees

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

        # The same bounded compile the local backend applies (a bad pattern
        # is a BadRequestError before anything is walked).
        compile_grep_pattern(args)

        result: dict | None = None
        if info.kind == "dir":
            search = getattr(self._sandbox, "search", None)
            if search is not None:
                # One recursive search in the sandbox (protocol 1.8) with a
                # file/size/match/output/time cap (review 2026-11-23, 5.11).
                # The reserved trees are skipped unless the search starts
                # inside one, as the legacy walk does. A runtime that answers
                # EUNSUPPORTED (an older image) falls back to the legacy
                # walk below.
                inside = reserved_tree(
                    workspace_relative(self._root, target_abs), self._reserved
                )
                try:
                    result = await search(
                        target_abs,
                        args.pattern,
                        glob=args.glob,
                        output_mode=args.output_mode,
                        context=args.context,
                        multiline=args.multiline,
                        case_insensitive=args.case_insensitive,
                        skip=() if inside else self._reserved,
                        max_files=_MAX_FILES,
                        max_file_bytes=_MAX_FILE_BYTES,
                        max_matches=_MAX_MATCHES,
                        head_limit=args.head_limit,
                        max_output_bytes=_OUTPUT_BYTES,
                        timeout_s=_DEADLINE_S,
                    )
                except RuntimeOpError as exc:
                    if exc.code != ErrorCode.EUNSUPPORTED:
                        raise
                    result = None
        if result is not None:
            out_lines: list[str] = list(result.get("lines") or [])
            truncated = bool(result.get("truncated", False))
            if args.head_limit is not None and len(out_lines) > args.head_limit:
                out_lines = out_lines[: args.head_limit]
                truncated = True
            # Same match_count/file_count/truncated metadata as the local
            # backend's grep.py, so a "searched N files" chip reads
            # identically regardless of which workspace backend ran the
            # call (truncated also covers the sandbox caps).
            notes = _stop_notes(result, args.glob)
            output = "\n".join(out_lines + [f"[note: {n}]" for n in notes])
            return ToolResult(
                output=output,
                truncated=truncated,
                metadata={
                    "match_count": int(result.get("match_count", 0)),
                    "file_count": int(result.get("file_count", 0)),
                    "truncated": truncated,
                },
            )

        # Legacy per-file walk: a single-file target, a pattern the runtime
        # cannot compile (regex-engine only), or a sandbox without search.
        rx = compile_grep_pattern(args)

        files: list[str] = []
        if info.kind == "file":
            files.append(target_abs)
        else:
            # The walk skips the reserved trees (one read RPC per file, git objects included), unless it starts inside one.
            inside = reserved_tree(workspace_relative(self._root, target_abs), self._reserved)
            await self._collect_files(target_abs, args.glob, files, () if inside else self._reserved, target_abs)

        out_lines: list[str] = []
        match_count = 0
        file_count = 0
        budget = grep_budget()
        for path_abs in files:
            try:
                body = await self._sandbox.read_file(path_abs)
            except OSError:
                continue
            except RuntimeOpError as exc:
                if exc.code not in _UNREADABLE:
                    raise
                continue
            if b"\x00" in body[:_BINARY_SNIFF_BYTES]:
                continue
            text = body.decode("utf-8", errors="replace")
            rel = workspace_relative(self._root, path_abs)
            try:
                lines, count = await run_bulk(
                    grep_text,
                    text,
                    rel,
                    rx=rx,
                    mode=args.output_mode,
                    context=args.context,
                    multiline=args.multiline,
                    budget=budget,
                )
            except UserRegexTimeout as exc:
                raise grep_budget_error(budget, exc) from exc
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
        skip: tuple[str, ...],
        base: str,
    ) -> None:
        for entry in await self._sandbox.list_dir(dir_abs):
            child_abs = f"{dir_abs}/{entry.path}"
            if skip and reserved_tree(workspace_relative(self._root, child_abs), skip):
                continue
            if entry.kind == "dir":
                await self._collect_files(child_abs, glob_filter, out, skip, base)
                continue
            if entry.kind != "file":
                continue
            if glob_filter is not None and not glob_matches(workspace_relative(base, child_abs), glob_filter):
                continue
            out.append(child_abs)


__all__ = ["SandboxGrep"]
