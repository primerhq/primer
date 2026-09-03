"""``ls`` for the sandbox backend."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

from primer.int.sandbox import FileStat, Sandbox
from primer.model.chat import ToolExample
from primer.model.except_ import BadRequestError, NotFoundError
from primer.workspace.local.tools.ls import LsArgs
from primer.workspace.sandbox.tools._common import resolve_sandbox_path
from primer.workspace.tool import ToolCallContext, ToolResult, WorkspaceTool


# 01a065f1: bounds a recursive=True walk's own entry count - max_depth
# alone does not (a wide-but-shallow tree is uncapped by depth). Kept
# at the same magnitude as primer.workspace.local.tools.ls.Ls's cap
# (01a0645c) for the same reason: this tool has no offset/limit at all,
# so the cap also bounds how much plain-text output lands in the
# model's context in one shot, not just walk cost.
_MAX_ENTRIES = 1_000


def _basename(path: str) -> str:
    # The real container/k8s runtime's list_dir returns each entry's
    # ABSOLUTE path (/workspace/foo); FakeSandbox and (per SandboxWorkspace's
    # own _walk, which this mirrors) possibly other backends return a bare
    # basename. Take the basename either way so descending doesn't
    # double-anchor the workspace root.
    return path.rsplit("/", 1)[-1]


async def _walk(
    sandbox: Sandbox,
    root_abs: str,
    *,
    show_hidden: bool,
    recursive: bool,
    max_depth: int | None,
    max_entries: int,
) -> list[tuple[str, FileStat]]:
    """Collect (path-relative-to-root, FileStat) pairs.

    Mirrors primer.workspace.local.tools.ls.Ls's own _walk/_visit: the
    entry-count cap stops BOTH appending and descending into further
    subdirectories the moment it's hit, not just a post-hoc slice, and
    non-recursive callers get the exact same hidden-file filtering
    recursive ones do (a single _visit(root_abs, "", 0) call with
    recursive=False never looks past its one iterdir-equivalent).
    """
    out: list[tuple[str, FileStat]] = []

    async def _visit(dir_abs: str, rel_prefix: str, depth: int) -> bool:
        for fs in await sandbox.list_dir(dir_abs):
            name = _basename(fs.path)
            if not show_hidden and name.startswith("."):
                continue
            if len(out) >= max_entries:
                return False
            rel = f"{rel_prefix}/{name}" if rel_prefix else name
            out.append((rel, fs))
            if recursive and fs.kind == "dir":
                if max_depth is None or depth + 1 < max_depth:
                    child_abs = f"{dir_abs}/{name}"
                    if not await _visit(child_abs, rel, depth + 1):
                        return False
        return True

    await _visit(root_abs, "", 0)
    return out


class SandboxLs(WorkspaceTool):
    """``ls``: list directory contents inside a sandbox."""

    id: ClassVar[str] = "ls"
    # Kept byte-identical to primer.workspace.local.tools.ls.Ls's
    # description (drift guard: tests/toolset/test_local_tool_
    # descriptions.py) - an agent sees the same guidance for the "ls"
    # tool id regardless of workspace backend.
    description: ClassVar[str] = (
        "List the contents of a directory. Returns one entry per line "
        "with kind, size, mtime, and name. A recursive walk over a very "
        "large tree is capped; a trailing line notes it and the "
        "listing is then partial, not exhaustive.\n\n"
        "Use when you need a directory listing; not for file contents "
        "(use ``read``)."
    )
    examples: ClassVar[list[ToolExample]] = [
        ToolExample(args={"path": "src"}, returns="entries in src"),
        ToolExample(
            args={"path": ".", "recursive": True},
            returns="recursive listing",
        ),
    ]

    def __init__(self, sandbox: Sandbox, *, workspace_root: str) -> None:
        self._sandbox = sandbox
        self._root = workspace_root

    def parameters(self) -> type[BaseModel]:
        return LsArgs

    async def execute(
        self, args: BaseModel, ctx: ToolCallContext,
    ) -> ToolResult:
        del ctx
        assert isinstance(args, LsArgs)
        target = resolve_sandbox_path(self._root, args.path)
        info = await self._sandbox.stat(target)
        if info is None:
            raise NotFoundError(f"{args.path!r} not found")
        if info.kind != "dir":
            raise BadRequestError(f"{args.path!r} is not a directory")

        # Over-fetch by one so a truncated walk is detected precisely
        # (len(results) > _MAX_ENTRIES) rather than guessed from
        # equality, which would also misfire when a tree's true entry
        # count happens to equal the cap exactly.
        results = await _walk(
            self._sandbox, target,
            show_hidden=args.show_hidden, recursive=args.recursive,
            max_depth=args.max_depth, max_entries=_MAX_ENTRIES + 1,
        )
        truncated = len(results) > _MAX_ENTRIES
        if truncated:
            results = results[:_MAX_ENTRIES]
        lines = []
        for rel, fs in results:
            mtime = fs.modified_at.strftime("%Y-%m-%dT%H:%M:%SZ")
            lines.append(
                f"{fs.kind:<7} {fs.size_bytes:>10} {mtime} {rel}"
            )
        if truncated:
            lines.append(
                f"... truncated at {_MAX_ENTRIES} entries (of unknown more)"
            )
        return ToolResult(output="\n".join(lines))


__all__ = ["SandboxLs"]
