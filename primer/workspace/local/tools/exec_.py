"""``exec`` -- run a shell command inside the workspace.

Module name is ``exec_`` (with a trailing underscore) because ``exec``
is a Python builtin function. The tool's ``id`` is still ``"exec"``,
which is what the LLM sees.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from primer.common.process_group import NEW_SESSION, kill_process_group
from primer.model.chat import ToolExample
from primer.model.except_ import BadRequestError, NotFoundError
from primer.workspace._locks import WorkspaceLockTable
from primer.workspace.tool import ToolCallContext, ToolResult, WorkspaceTool
from primer.workspace.local.tools._common import (
    TOOL_RESULT_CAP_BYTES,
    _curated_subprocess_env,
    resolve_workspace_path,
)


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
    check: bool = Field(
        default=False,
        description=(
            "Fail the tool call when the command exits non-zero. Default False "
            "keeps the historical behaviour, where a failing command returns "
            "normally and its exit code is visible only in the output text - so "
            "a graph node wrapping it reports success no matter what happened "
            "inside. Set True for any step whose failure should stop the graph."
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

    Foreground output: ``<exit code>\\n<stdout>\\n<stderr>``. Each stream
    is capped at its head (``_HEAD_BYTES``) plus its tail (``_TAIL_BYTES``)
    with a ``[... N bytes omitted ...]`` marker between, and the tool sets
    :attr:`ToolResult.truncated` whenever bytes were omitted so the
    runtime skips its outer truncation instead of double-cutting.

    On timeout: kills the process group, raises :class:`BadRequestError`.

    Background mode is not yet implemented (per spec: "a separate
    ``exec_status(pid)`` companion -- TBD in implementation"). Today
    it raises :class:`BadRequestError`.
    """

    self_bounded: ClassVar[bool] = True
    id: ClassVar[str] = "exec"
    description: ClassVar[str] = (
        "Run a shell command in the workspace. Returns "
        "<exit code>\\n<stdout>\\n<stderr>. Use the optional "
        "timeout_ms to bound runtime.\n\n"
        "Use when you need to run a program or inspect the environment; "
        "not for reading one file (use ``read``).\n\n"
        "Optional access='read' skips write-locking for read-only commands "
        "so they run fully parallel; optional writes=[path, ...] narrows "
        "the write-lock to those specific paths instead of the whole "
        "working directory. Writers to the same directory serialize; "
        "writers to different directories run in parallel."
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
            # Its own session, so a timeout or a cancel can kill the WHOLE process group (see
            # primer.common.process_group): the shell forks, and killing only the shell left its children running
            # (and the timeout waiting for them, since they hold the pipes). The lock is released only after the
            # kill, so a stopped command no longer runs on without it.
            proc = await asyncio.create_subprocess_shell(
                args.command,
                cwd=str(cwd),
                env=proc_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **NEW_SESSION,
            )
            finished = False
            try:
                stdout, stderr, dropped = await asyncio.wait_for(
                    _communicate_capped(proc),
                    timeout=args.timeout_ms / 1000.0,
                )
                finished = True
            except TimeoutError as exc:
                raise BadRequestError(
                    f"command timed out after {args.timeout_ms}ms: {args.command!r}"
                ) from exc
            finally:
                # Any way out of the wait that is not the command finishing: the timeout, a cancel, or anything else
                # (a failure inside the wait). Not keyed on ``proc.returncode``: the shell may already be gone while a
                # job it left behind holds the pipes, and that job is still in the group.
                if not finished:
                    await kill_process_group(proc)

        rc = proc.returncode if proc.returncode is not None else -1
        out_text = stdout.decode("utf-8", errors="replace")
        err_text = stderr.decode("utf-8", errors="replace")
        body = f"{rc}\n{out_text}\n{err_text}"
        if dropped:
            body += (
                f"\n[output truncated: {dropped} bytes were omitted "
                "from the streams above]"
            )
        if args.check and rc != 0:
            raise BadRequestError(nonzero_exit_message(args.command, rc, out_text, err_text))
        # truncated=True tells the runtime the tool already bounded its
        # output, so it must not apply the outer 50 KiB truncation on top.
        return ToolResult(
            output=body,
            truncated=dropped > 0,
            metadata={
                "exit_code": rc,
                "command": args.command,
                "workdir": args.workdir,
            },
        )


# ===========================================================================
# Helpers
# ===========================================================================


_CHUNK = 64 * 1024

#: Per-stream retention for the exec cap: the first ``_HEAD_BYTES`` and
#: the last ``_TAIL_BYTES`` of EACH stream (head + tail =
#: ``TOOL_RESULT_CAP_BYTES``), so one stream's full budget cannot starve
#: the other: the tail of a failing build's stderr survives whatever
#: stdout printed.
_HEAD_BYTES = 40 * 1024
_TAIL_BYTES = TOOL_RESULT_CAP_BYTES - _HEAD_BYTES


def _assemble_stream(head: bytes, tail: bytes, dropped: int) -> bytes:
    """Head + omitted-marker + tail for one stream; the plain stream when
    nothing was dropped."""
    if dropped == 0:
        # The whole stream fit its head + tail budgets: head holds the
        # first bytes, tail the rest (empty for a head-sized stream), so
        # both are part of the kept output.
        return head + tail
    return (
        head
        + b"\n[... "
        + str(dropped).encode("ascii")
        + b" bytes omitted ...]\n"
        + tail
    )


async def _communicate_capped(
    proc: asyncio.subprocess.Process,
) -> tuple[bytes, bytes, int]:
    """Drain both pipes to EOF, keeping per stream at most ``_HEAD_BYTES``
    (its head) and ``_TAIL_BYTES`` (its tail).

    An unbounded ``proc.communicate()`` would hold a printing command's
    WHOLE output (a ``cat`` of a multi-gigabyte log, a printing loop) in
    the API/worker process. A pipe has no RLIMIT_FSIZE, so both streams
    must still be drained to EOF after their budgets are spent -- or the
    child blocks on a full pipe and only the timeout ends it -- and the
    excess middle is counted and discarded. A SHARED combined budget
    would let one stream starve the other (50 KiB of stdout erasing a
    failing stderr), so each stream keeps its own head + tail. Returns
    ``(stdout, stderr, dropped_bytes)``; each stream already carries the
    ``[... N bytes omitted ...]`` marker when its middle was dropped.
    """

    async def _drain(stream) -> tuple[bytes, bytes, int]:
        head = bytearray()
        tail = bytearray()
        dropped = 0
        while True:
            chunk = await stream.read(_CHUNK)
            if not chunk:
                break
            room = _HEAD_BYTES - len(head)
            if room > 0:
                head += chunk[:room]
                chunk = chunk[room:]
            if chunk:
                # Count only the bytes actually discarded: the tail keeps
                # its own last _TAIL_BYTES, and those are NOT dropped
                # (counting them overstated the omission by up to the tail
                # budget and marked a head-sized stream as truncated).
                troom = _TAIL_BYTES - len(tail)
                if troom < len(chunk):
                    dropped += len(chunk) - troom
                tail += chunk
                del tail[:-_TAIL_BYTES]
        return bytes(head), bytes(tail), dropped

    out_task = asyncio.ensure_future(_drain(proc.stdout))
    err_task = asyncio.ensure_future(_drain(proc.stderr))
    try:
        await asyncio.gather(out_task, err_task)
        await proc.wait()
    finally:
        out_task.cancel()
        err_task.cancel()
    out_head, out_tail, out_dropped = out_task.result()
    err_head, err_tail, err_dropped = err_task.result()
    return (
        _assemble_stream(out_head, out_tail, out_dropped),
        _assemble_stream(err_head, err_tail, err_dropped),
        out_dropped + err_dropped,
    )


def nonzero_exit_message(command: str, rc: int, stdout: str, stderr: str) -> str:
    """A failure message carrying the tail of the output, not just the code.

    The exit code alone sends the reader back to the raw output to find out what
    happened; including the tail means the node's error field says it directly.
    stderr first because that is where a traceback lands.
    """
    tail = (stderr.strip() or stdout.strip())[-800:]
    suffix = f"\n{tail}" if tail else ""
    return f"command exited {rc}: {command!r}{suffix}"


__all__ = ["Exec", "ExecArgs", "nonzero_exit_message"]
