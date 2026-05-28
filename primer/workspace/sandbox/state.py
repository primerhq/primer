"""SandboxStateRepo -- git-backed state repo inside a Sandbox.

Same contract as :class:`matrix.workspace.local.state.LocalStateRepo`
but every git op dispatches via :class:`Sandbox.exec` in argv form
(no shell). Files are materialised into the sandbox via
:class:`Sandbox.write_file`.

Baseline image requirement: ``git`` must be installed in the container
image. :meth:`initialize` raises :class:`RuntimeError` if git is
missing.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from primer.int.sandbox import Sandbox
from primer.model.workspace import CommitInfo, Op


logger = logging.getLogger(__name__)


_TRAILER_WORKSPACE = "X-Matrix-Workspace"
_TRAILER_SESSION = "X-Matrix-Session"
_TRAILER_AGENT = "X-Matrix-Agent"
_TRAILER_OP = "X-Matrix-Op"
_TRAILER_TOOL = "X-Matrix-Tool"
_TRAILER_CALL = "X-Matrix-Call"


class SandboxStateRepo:
    """Git-backed state repo, dispatching every op through a Sandbox.

    All shell-level work is broken into individual argv-form
    :class:`Sandbox.exec` calls so the impl is portable across runtimes
    that may not have a particular shell.
    """

    def __init__(
        self,
        sandbox: Sandbox,
        *,
        state_path: str,
        workspace_id: str,
    ) -> None:
        if not workspace_id:
            raise ValueError("workspace_id must be non-empty")
        self._sandbox = sandbox
        self._state_path = state_path
        self._workspace_id = workspace_id
        self._lock = asyncio.Lock()

    @property
    def workspace_id(self) -> str:
        return self._workspace_id

    @property
    def state_path(self) -> str:
        return self._state_path

    async def initialize(self) -> None:
        """Create the state directory and git-init it (idempotent)."""
        # Ensure the state dir exists. write_file auto-creates parents;
        # we then delete the sentinel.
        sentinel = f"{self._state_path}/.matrix-init"
        await self._sandbox.write_file(sentinel, b"")
        await self._sandbox.delete(sentinel)

        # Idempotent: if .git is already there, nothing to do.
        existing = await self._sandbox.stat(f"{self._state_path}/.git")
        if existing is not None:
            return

        # git init
        r = await self._sandbox.exec(
            ["git", "init", "--quiet"], workdir=self._state_path,
        )
        if r.exit_code != 0:
            raise RuntimeError(
                "git init failed in sandbox -- is git installed in the image?\n"
                f"stderr: {r.stderr}"
            )
        # Local repo author config so commits don't depend on global git.
        await self._sandbox.exec(
            ["git", "config", "user.email", "matrix@local"],
            workdir=self._state_path,
        )
        await self._sandbox.exec(
            ["git", "config", "user.name", "matrix"],
            workdir=self._state_path,
        )
        # Initial empty commit so HEAD exists.
        r = await self._sandbox.exec(
            ["git", "commit", "--allow-empty", "--quiet", "-m", "init"],
            workdir=self._state_path,
        )
        if r.exit_code != 0:
            raise RuntimeError(
                f"git initial commit failed (rc={r.exit_code}): {r.stderr}"
            )

    async def commit_turn(
        self,
        *,
        session_id: str,
        op: Op,
        agent_id: str,
        message_body: str,
        files: dict[str, bytes],
        tool_id: str | None = None,
        call_id: str | None = None,
    ) -> str:
        """Stage files under sessions/<session_id>/, commit with trailers,
        return the new SHA."""
        async with self._lock:
            for rel, content in files.items():
                target = f"{self._state_path}/sessions/{session_id}/{rel}"
                await self._sandbox.write_file(target, content)

            trailers: dict[str, str] = {
                _TRAILER_WORKSPACE: self._workspace_id,
                _TRAILER_SESSION: session_id,
                _TRAILER_AGENT: agent_id,
                _TRAILER_OP: op,
            }
            if tool_id is not None:
                trailers[_TRAILER_TOOL] = tool_id
            if call_id is not None:
                trailers[_TRAILER_CALL] = call_id
            full_msg = _build_commit_message(message_body, trailers)

            r = await self._sandbox.exec(
                ["git", "add", "--", f"sessions/{session_id}"],
                workdir=self._state_path,
            )
            if r.exit_code != 0:
                raise RuntimeError(
                    f"git add failed (rc={r.exit_code}): {r.stderr}"
                )
            r = await self._sandbox.exec(
                ["git", "commit", "--quiet", "-F", "-"],
                workdir=self._state_path,
                stdin=full_msg.encode("utf-8"),
            )
            if r.exit_code != 0:
                raise RuntimeError(
                    f"git commit failed (rc={r.exit_code}): {r.stderr}"
                )
            sha_res = await self._sandbox.exec(
                ["git", "rev-parse", "HEAD"], workdir=self._state_path,
            )
            return sha_res.stdout.strip()

    async def history(self, *, limit: int = 50) -> list[CommitInfo]:
        """Return up to ``limit`` recent commits, newest first."""
        result = await self._sandbox.exec(
            [
                "git", "log", f"--max-count={limit}",
                "--pretty=format:%H%x1f%s%x1f%ct%x1f%(trailers:only,unfold)%x1e",
                "--no-color",
            ],
            workdir=self._state_path,
        )
        return _parse_log(result.stdout)


def _build_commit_message(subject: str, trailers: dict[str, str]) -> str:
    """Subject + blank line + ``Key: value`` trailer lines + trailing newline."""
    parts = [subject, ""]
    for key, value in trailers.items():
        parts.append(f"{key}: {value}")
    return "\n".join(parts) + "\n"


_RECORD_SEP = "\x1e"
_FIELD_SEP = "\x1f"


def _parse_log(text: str) -> list[CommitInfo]:
    out: list[CommitInfo] = []
    for record in text.split(_RECORD_SEP):
        record = record.strip("\n")
        if not record:
            continue
        parts = record.split(_FIELD_SEP)
        if len(parts) < 4:
            continue
        sha, subject, ct, trailers_raw = parts[0], parts[1], parts[2], parts[3]
        trailers: dict[str, str] = {}
        for line in trailers_raw.splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            key, _, value = line.partition(":")
            trailers[key.strip()] = value.strip()
        out.append(CommitInfo(
            sha=sha,
            subject=subject,
            committed_at=datetime.fromtimestamp(int(ct), tz=timezone.utc),
            workspace_id=trailers.get(_TRAILER_WORKSPACE),
            session_id=trailers.get(_TRAILER_SESSION),
            agent_id=trailers.get(_TRAILER_AGENT),
            op=trailers.get(_TRAILER_OP),
            tool=trailers.get(_TRAILER_TOOL),
            call_id=trailers.get(_TRAILER_CALL),
        ))
    return out


__all__ = ["SandboxStateRepo"]
