"""``exec`` -- run a shell command inside the workspace.

Module name is ``exec_`` (with a trailing underscore) because ``exec``
is a Python builtin function. The tool's ``id`` is still ``"exec"``,
which is what the LLM sees.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from matrix.model.except_ import BadRequestError, NotFoundError
from matrix.workspace.tool import ToolCallContext, ToolResult, WorkspaceTool
from matrix.workspace.tools._common import resolve_workspace_path


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
        "timeout_ms to bound runtime."
    )

    def __init__(
        self,
        workspace_root: Path,
        *,
        env: dict[str, str] | None = None,
    ) -> None:
        """Construct.

        ``env`` -- if provided, these variables are merged on top of
        the current process environment for every spawned shell. Pass
        ``None`` (the default) to inherit the current environment
        unchanged. Used by :class:`LocalWorkspace` to inject the
        ``WorkspaceTemplate.env`` overrides.
        """
        self._root = Path(workspace_root)
        self._env = env

    def parameters(self) -> type[BaseModel]:
        return ExecArgs

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

        proc_env: dict[str, str] | None = None
        if self._env is not None:
            proc_env = {**os.environ, **self._env}
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
        except asyncio.TimeoutError as exc:
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


__all__ = ["Exec", "ExecArgs"]
