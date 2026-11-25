"""The claude_code child process layer.

The shared agent-runtime helpers (``primer.llm.agent_runtime.process``) with
claude_code's parameters: its temp-directory prefix (``primer-cc``) and its
MCP tool bridge idle-timeout override. The public names are re-exported so the
old import paths keep working. The crash sweep owns only ``primer-cc`` directories.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

from primer.llm.agent_runtime.process import (
    ALLOWED,
    CLEANUP_WAIT_S,
    _ChildRecord,
    _STREAM_LINE_LIMIT,
    _child_pgid,
    child_env as _shared_child_env,
    child_process as _shared_child_process,
    read_stderr_tail,
    sweep_orphans as _shared_sweep_orphans,
)

__all__ = [
    "ALLOWED",
    "CHILD_IDLE_TIMEOUT_ENV",
    "CLEANUP_WAIT_S",
    "_ChildRecord",
    "_child_pgid",
    "child_env",
    "child_process",
    "read_stderr_tail",
    "sweep_orphans",
]

#: Temp-directory name prefix for this provider's crash-sweep entries
#: (keyed by the WORKER pid, like the shared sweep expects).
TMP_PREFIX = "primer-cc"

#: The child's MCP tool bridge must not idle-timeout mid-stream.
CHILD_IDLE_TIMEOUT_ENV = "CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT"


async def sweep_orphans(tmp_root: Path | None = None) -> int:
    """Clean up Claude children of dead workers."""
    return await _shared_sweep_orphans(tmp_root, prefix=TMP_PREFIX)


def child_env(pass_env: Sequence[str]) -> dict[str, str]:
    """The claude_code child environment: the shared scrubbed env plus the MCP
    tool bridge idle-timeout override. Nothing else."""
    return _shared_child_env(pass_env, extra={CHILD_IDLE_TIMEOUT_ENV: "0"})


@asynccontextmanager
async def child_process(
    argv_without_tmp: Callable[[Path], list[str]],
    *,
    env: dict[str, str],
    system_text: str,
) -> AsyncIterator[asyncio.subprocess.Process]:
    """The claude_code child: the shared agent-runtime child with this
    provider's temp-directory prefix and its ``system.md`` file (the system
    prompt). See :func:`primer.llm.agent_runtime.process.child_process`. The
    stdout readline limit is this module's ``_STREAM_LINE_LIMIT`` (read at
    call time, so a test that patches it here takes effect)."""
    async with _shared_child_process(
        argv_without_tmp, env=env, prefix=TMP_PREFIX, files={"system.md": system_text},
        line_limit=_STREAM_LINE_LIMIT,
    ) as proc:
        yield proc
