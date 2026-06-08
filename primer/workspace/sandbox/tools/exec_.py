"""``exec`` for the sandbox backend.

Module name is ``exec_`` (trailing underscore) because ``exec`` is a
Python builtin. The tool's ``id`` is still ``"exec"``.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

from primer.int.sandbox import Sandbox
from primer.model.chat import ToolExample
from primer.model.except_ import BadRequestError
from primer.workspace.local.tools.exec_ import ExecArgs
from primer.workspace.sandbox.tools._common import resolve_sandbox_path
from primer.workspace.tool import ToolCallContext, ToolResult, WorkspaceTool


class SandboxExec(WorkspaceTool):
    """Run a shell command via the sandbox.

    Output: ``<exit code>\\n<stdout>\\n<stderr>``. On timeout, kills the
    process and raises :class:`BadRequestError`. ``ctx.abort`` is wired
    through to the underlying :meth:`Sandbox.exec`.
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

    def __init__(self, sandbox: Sandbox, *, workspace_root: str) -> None:
        self._sandbox = sandbox
        self._root = workspace_root

    def parameters(self) -> type[BaseModel]:
        return ExecArgs

    async def execute(
        self, args: BaseModel, ctx: ToolCallContext,
    ) -> ToolResult:
        assert isinstance(args, ExecArgs)
        if args.background:
            raise BadRequestError(
                "background=true is not yet supported"
            )
        cwd_abs = resolve_sandbox_path(self._root, args.workdir)
        try:
            result = await self._sandbox.exec(
                args.command,
                workdir=cwd_abs,
                timeout_seconds=args.timeout_ms / 1000.0,
                abort=ctx.abort,
            )
        except TimeoutError as exc:
            raise BadRequestError(
                f"command timed out after {args.timeout_ms}ms: "
                f"{args.command!r}"
            ) from exc
        body = f"{result.exit_code}\n{result.stdout}\n{result.stderr}"
        return ToolResult(
            output=body,
            metadata={
                "exit_code": result.exit_code,
                "command": args.command,
                "workdir": args.workdir,
            },
        )


__all__ = ["SandboxExec"]
