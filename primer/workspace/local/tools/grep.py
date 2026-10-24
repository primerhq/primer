"""``grep`` -- regex search across workspace files.

The pattern is the agent's (G6-4): it is checked and compiled through
:mod:`primer.common.user_regex` (at most 1000 characters, the engine-parsed
repeat cap, the ``regex`` engine, never cached) and matched on the user-regex
bulk executor with the GIL released, under one :data:`GREP_MATCH_SECONDS` budget
per call and a :data:`GREP_PER_SEARCH_SECONDS` bound per line search, so a
catastrophic pattern ends in an error that names its cause instead of
stalling every session on the worker. An ordinary pattern is matched with
one timed scan per file (:func:`~primer.common.user_regex.line_scanner`).
The sandbox backend shares :func:`compile_grep_pattern` and :func:`grep_text`.
"""

from __future__ import annotations

import asyncio
import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal

import regex
from pydantic import BaseModel, Field

from primer.common.user_regex import (
    MAX_PATTERN_LENGTH,
    MatchBudget,
    UserRegexError,
    UserRegexTimeout,
    compile_user_regex,
    line_scanner,
    matching_lines,
    run_bulk,
)
from primer.model.chat import ToolExample
from primer.model.except_ import BadRequestError, NotFoundError
from primer.workspace.tool import ToolCallContext, ToolResult, WorkspaceTool
from primer.workspace.local.tools._common import resolve_workspace_path, workspace_relative


_BINARY_SNIFF_BYTES = 1024
_DEFAULT_HEAD_LIMIT = 250
# The total time the regex engine may spend on one grep call, over every
# file it reads (file reads and the directory walk do not count).
GREP_MATCH_SECONDS = 10.0
# The longest one line search may run (a whole-file scan is bounded by the total).
GREP_PER_SEARCH_SECONDS = 2.0


class GrepArgs(BaseModel):
    """Arguments for the ``grep`` tool."""

    pattern: str = Field(
        ...,
        min_length=1,
        description=f"Regular expression (at most {MAX_PATTERN_LENGTH} characters).",
    )
    path: str = Field(
        default=".",
        description="File or directory to search (relative to the workspace root).",
    )
    glob: str | None = Field(
        default=None,
        description="Filter files by glob (e.g. '*.py').",
    )
    output_mode: Literal["content", "files_with_matches", "count"] = Field(
        default="files_with_matches",
        description=(
            "'content' = <path>:<lineno>:<text>; "
            "'files_with_matches' = one path per line; "
            "'count' = <path>:<n>."
        ),
    )
    case_insensitive: bool = Field(default=False)
    multiline: bool = Field(
        default=False,
        description="Allow . to match newlines (re.DOTALL) and ^/$ to match each line (re.MULTILINE).",
    )
    context: int = Field(
        default=0,
        ge=0,
        description="Lines of context around each match (only honoured by 'content' mode).",
    )
    head_limit: int | None = Field(
        default=_DEFAULT_HEAD_LIMIT,
        ge=1,
        description="Cap on output lines. Pass null for no cap.",
    )


class Grep(WorkspaceTool):
    """Find content in workspace files.

    Pure-Python implementation: walks files under ``path`` (or treats
    ``path`` as a single file), applies the optional file ``glob``
    filter, and matches each line (or each whole-file blob in
    multiline mode) against the supplied regex. Output shape depends
    on ``output_mode``.

    Skips binary files (NUL-byte sniff) silently.

    Sets :attr:`ToolResult.truncated` to True when the head_limit cap
    fires so the runtime skips outer truncation.
    """

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

    def __init__(self, workspace_root: Path) -> None:
        self._root = Path(workspace_root)

    def parameters(self) -> type[BaseModel]:
        return GrepArgs

    async def execute(self, args: BaseModel, ctx: ToolCallContext) -> ToolResult:
        del ctx
        assert isinstance(args, GrepArgs)
        target = resolve_workspace_path(self._root, args.path)
        if not target.exists():
            raise NotFoundError(f"{args.path!r} not found")

        rx = compile_grep_pattern(args)
        files = await asyncio.to_thread(_collect_files, target, args.glob)
        budget = grep_budget()
        try:
            lines, match_count, file_count = await run_bulk(
                _grep_files,
                files,
                rx=rx,
                mode=args.output_mode,
                context=args.context,
                multiline=args.multiline,
                workspace_root=self._root,
                budget=budget,
            )
        except UserRegexTimeout as exc:
            raise grep_budget_error(budget, exc) from exc

        capped: list[str]
        truncated = False
        if args.head_limit is not None and len(lines) > args.head_limit:
            capped = lines[: args.head_limit]
            truncated = True
        else:
            capped = lines

        # UX reconcile wave 5: match_count/file_count used to be computed
        # here and thrown away (`del full_count`) - a chip that wants to
        # say "searched N files" (files_with_matches, the default mode)
        # or "N matches" (count/content) needs the EXACT number, not a
        # client-side parse of a capped, mode-shaped output string.
        # truncated is duplicated into metadata (also on ToolResult itself)
        # so a UI reading only `metadata` still knows to caveat the count
        # ("250+ files") instead of presenting a capped total as exact.
        return ToolResult(
            output="\n".join(capped),
            truncated=truncated,
            metadata={
                "match_count": match_count,
                "file_count": file_count,
                "truncated": truncated,
            },
        )


@dataclass(frozen=True)
class GrepPattern:
    """A grep pattern, compiled, with its one-scan-per-file line scanner when the pattern allows one."""

    rx: regex.Pattern
    scanner: regex.Pattern | None = None


def compile_grep_pattern(args: GrepArgs) -> GrepPattern:
    """The bounded, compiled pattern for ``args``; a refusal is a ``BadRequestError`` (an error result to the model).

    A one-shot pattern: neither it nor its scanner enters the long-lived
    pattern cache.
    """
    flags = 0
    if args.case_insensitive:
        flags |= regex.IGNORECASE
    if args.multiline:
        flags |= regex.DOTALL | regex.MULTILINE
    try:
        rx = compile_user_regex(args.pattern, flags, what="grep pattern")
    except UserRegexError as exc:
        raise BadRequestError(str(exc)) from None
    scanner = None if args.multiline else line_scanner(args.pattern, flags, what="grep pattern")
    return GrepPattern(rx=rx, scanner=scanner)


class GrepBudget(MatchBudget):
    """The budget of one grep call, and where it had got to (for the error)."""

    def __init__(self) -> None:
        super().__init__(GREP_MATCH_SECONDS, per_search=GREP_PER_SEARCH_SECONDS, what="grep pattern")
        self.lines_done = 0
        self.where: tuple[str, int] | None = None


def grep_budget() -> GrepBudget:
    return GrepBudget()


def grep_budget_error(budget: MatchBudget, exc: UserRegexTimeout | None = None) -> BadRequestError:
    """The error a timed-out grep answers, worded by its cause: one search past its bound, or the whole budget spent."""
    where = getattr(budget, "where", None)
    at = f" at {where[0]} line {where[1]}" if where else ""
    if exc is not None and exc.cause == "per_search" and not budget.exhausted:
        return BadRequestError(
            f"grep stopped{at}: one search ran past the {budget.per_search:g} s per-search bound. "
            "Overlapping or nested repeats such as (a|aa)+ or (x+)+ backtrack on long lines; "
            "simplify the pattern."
        )
    lines_done = getattr(budget, "lines_done", None)
    after = f" after {lines_done} lines" if lines_done is not None else ""
    return BadRequestError(
        f"grep stopped{after}{at}: matching used its {budget.seconds:g} s matching budget "
        f"(the longest single search took {budget.longest:.2f} s). If one search was long, "
        "simplify the pattern (overlapping or nested repeats such as (a|aa)+ backtrack on long "
        "lines); otherwise narrow path or glob."
    )


def grep_text(
    text: str,
    rel: str,
    *,
    rx: GrepPattern | regex.Pattern,
    mode: str,
    context: int,
    multiline: bool,
    budget: MatchBudget,
) -> tuple[list[str], int]:
    """The output lines for one file and its match count (``([], 0)`` when nothing matched).

    Raises :class:`~primer.common.user_regex.UserRegexTimeout` (a ``TimeoutError``) when ``budget`` runs out;
    a :class:`GrepBudget` then records the file, the line and the lines done.
    """
    pattern = rx if isinstance(rx, GrepPattern) else GrepPattern(rx=rx)
    out: list[str] = []
    if multiline:
        try:
            matches_ml = budget.findall_matches(pattern.rx, text)
        except UserRegexTimeout:
            if isinstance(budget, GrepBudget):
                budget.where = (rel, 1)
            raise
        if not matches_ml:
            return out, 0
        if mode == "files_with_matches":
            out.append(rel)
        elif mode == "count":
            out.append(f"{rel}:{len(matches_ml)}")
        else:
            for m in matches_ml:
                snippet = m.group(0).replace("\n", "\\n")
                out.append(f"{rel}::{snippet}")
        return out, len(matches_ml)

    all_lines = text.splitlines()
    try:
        hits = matching_lines(pattern.rx, all_lines, budget, scanner=pattern.scanner)
    except UserRegexTimeout as exc:
        if isinstance(budget, GrepBudget):
            line = getattr(exc, "line", 0)
            budget.where = (rel, line + 1)
            budget.lines_done += line
        raise
    if isinstance(budget, GrepBudget):
        budget.lines_done += len(all_lines)
    if not hits:
        return out, 0
    if mode == "files_with_matches":
        out.append(rel)
    elif mode == "count":
        out.append(f"{rel}:{len(hits)}")
    else:  # content mode
        emitted: set[int] = set()
        for index in hits:
            start = max(0, index - context)
            end = min(len(all_lines), index + 1 + context)
            for i in range(start, end):
                if i in emitted:
                    continue
                emitted.add(i)
                out.append(f"{rel}:{i + 1}:{all_lines[i]}")
    return out, len(hits)


def _collect_files(target: Path, glob_filter: str | None) -> list[Path]:
    if target.is_file():
        return [target]
    out: list[Path] = []
    for p in target.rglob("*"):
        if not p.is_file():
            continue
        if glob_filter is not None and not fnmatch.fnmatch(p.name, glob_filter):
            continue
        out.append(p)
    out.sort(key=lambda p: p.as_posix())
    return out


def _grep_files(
    files: list[Path],
    *,
    rx: GrepPattern,
    mode: str,
    context: int,
    multiline: bool,
    workspace_root: Path,
    budget: MatchBudget,
) -> tuple[list[str], int, int]:
    """Return (lines, total-match-count, files-with-a-match-count)."""
    out: list[str] = []
    total = 0
    file_count = 0
    for path in files:
        try:
            with path.open("rb") as fh:
                head = fh.read(_BINARY_SNIFF_BYTES)
        except OSError:
            continue
        if b"\x00" in head:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = workspace_relative(workspace_root, path)
        lines, count = grep_text(
            text, rel, rx=rx, mode=mode, context=context, multiline=multiline, budget=budget,
        )
        if count:
            total += count
            file_count += 1
            out.extend(lines)
    return out, total, file_count


__all__ = [
    "GREP_MATCH_SECONDS",
    "GREP_PER_SEARCH_SECONDS",
    "Grep",
    "GrepArgs",
    "GrepBudget",
    "GrepPattern",
    "compile_grep_pattern",
    "grep_budget",
    "grep_budget_error",
    "grep_text",
]
