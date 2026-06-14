"""Git-backed per-workspace state store, session-scoped (local-FS backend).

One repo per workspace, shared by every :class:`AgentSession` running on
that workspace. Each session owns the subdirectory
``sessions/<session_id>/`` inside the repo. One commit per assistant
turn per session captures every change to that session's slot in the
turn (transcript appends, todo writes, memory updates, status changes,
``waiting.json`` create / delete).

Commit messages carry trailers identifying workspace, session, agent,
op, and (when applicable) tool and call id, so the history is greppable
via standard git tooling::

    git log --grep='X-Primer-Session: sess-abc'
    git log --grep='X-Primer-Agent: agent-foo'
    git log --grep='X-Primer-Op: user_instruction'

Concurrency: a workspace-wide :class:`asyncio.Lock` serialises commits
so concurrent sessions don't fight over ``.git/index.lock``.

See ``docs/superpowers/specs/2026-05-02-workspace-design.md`` (the
"state layer" section) for the full design.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from pydantic import TypeAdapter

from primer.model.except_ import SubprocessTimeoutError
from primer.model.workspace_session import (
    AgentBinding,
    SessionInfo,
    WaitingState,
)
from primer.model.workspace import CommitInfo, Op


logger = logging.getLogger(__name__)


# ===========================================================================
# Constants
# ===========================================================================


_AUTHOR_NAME = "primer"
_AUTHOR_EMAIL = "primer@local"

# Trailer keys -- machine-readable identifiers in the commit body.
_TRAILER_WORKSPACE = "X-Primer-Workspace"
_TRAILER_SESSION = "X-Primer-Session"
_TRAILER_AGENT = "X-Primer-Agent"
_TRAILER_OP = "X-Primer-Op"
_TRAILER_TOOL = "X-Primer-Tool"
_TRAILER_CALL = "X-Primer-Call"

# Allowed values of the ``op`` trailer (canonical type lives in
# ``primer.model.workspace.Op``; this set is the runtime validator).
_VALID_OPS: frozenset[str] = frozenset(
    [
        "attach",
        "message",
        "user_instruction",
        "tool_call",
        "tool_result",
        "memory_write",
        "todo_update",
        "status_change",
    ]
)


# ===========================================================================
# LocalStateRepo
# ===========================================================================


_waiting_state_adapter: TypeAdapter[WaitingState] = TypeAdapter(WaitingState)


class LocalStateRepo:
    """Git-backed per-workspace state store, session-scoped, host-FS backed.

    Concurrency: every commit acquires the workspace's commit lock so
    parallel sessions don't fight over ``.git/index.lock``. Reads are
    not locked -- ``git log`` and ``git show`` operate on read-only
    snapshots.
    """

    def __init__(
        self,
        path: Path,
        *,
        workspace_id: str,
        subprocess_timeout_seconds: float = 120.0,
    ) -> None:
        if not workspace_id:
            raise ValueError("workspace_id must be non-empty")
        self._path = Path(path)
        self._workspace_id = workspace_id
        self._subprocess_timeout_seconds = subprocess_timeout_seconds
        self._commit_lock = asyncio.Lock()
        # session_id -> agent_id, populated on create_session and on
        # init scan. The commit-trailer assembler uses this so callers
        # don't have to thread agent_id through every commit call.
        self._agent_by_session: dict[str, str] = {}

    # ---- public surface --------------------------------------------------

    @property
    def path(self) -> Path:
        """The ``.state/`` directory backing this repo."""
        return self._path

    @property
    def workspace_id(self) -> str:
        """The workspace id stamped into every commit's trailer."""
        return self._workspace_id

    async def initialize(self) -> None:
        """Open / initialise the repo. Idempotent.

        Creates the directory if missing, runs ``git init`` if no
        ``.git/`` is present, and rebuilds the in-memory
        ``session_id -> agent_id`` cache by scanning every existing
        ``sessions/<session_id>/agent.json``.
        """
        await asyncio.to_thread(self._path.mkdir, parents=True, exist_ok=True)
        if not (self._path / ".git").exists():
            await self._run_git("init", "--initial-branch=main")
        await self._scan_existing_sessions()

    async def create_session(
        self,
        session_info: SessionInfo,
        agent_binding: AgentBinding,
    ) -> str:
        """Allocate the session slot, write session.json + agent.json, commit.

        Returns the SHA of the ``attach`` commit. The session id is
        taken from ``session_info.session_id`` and must be free of path
        separators (validated). Subsequent commits for this session can
        omit the agent_id -- it's cached from the binding.
        """
        session_id = session_info.session_id
        _validate_session_id(session_id)
        if session_id in self._agent_by_session:
            raise ValueError(f"session already exists: {session_id!r}")

        # Cache up-front so commit() can find the agent id for trailers.
        self._agent_by_session[session_id] = agent_binding.agent_id

        files: dict[str, str | bytes] = {
            "session.json": session_info.model_dump_json(indent=2),
            "agent.json": agent_binding.model_dump_json(indent=2),
        }
        try:
            return await self.commit(
                session_id,
                summary=f"{session_id}: attach",
                op="attach",
                files=files,
            )
        except Exception:
            # Roll back the cache entry so a retry can pass through again.
            self._agent_by_session.pop(session_id, None)
            raise

    async def commit(
        self,
        session_id: str,
        *,
        summary: str,
        op: Op,
        tool: str | None = None,
        call_id: str | None = None,
        files: dict[str, str | bytes] | None = None,
        delete_files: list[str] | None = None,
    ) -> str:
        """Stage files under sessions/<session_id>/, commit with trailers.

        ``files`` keys are paths relative to the session slot
        (e.g. ``"messages.jsonl"`` or ``"memory/note.md"``); values are
        the file contents. ``delete_files`` lists paths (also relative
        to the session slot) to ``git rm`` in the same commit -- used
        for removing ``waiting.json`` when transitioning out of WAITING.

        Returns the new commit's full SHA. Acquires the commit lock for
        the duration so concurrent commits serialise.
        """
        _validate_session_id(session_id)
        if op not in _VALID_OPS:
            raise ValueError(f"unknown op: {op!r}")

        agent_id = self._agent_by_session.get(session_id)
        if agent_id is None:
            raise LookupError(
                f"session {session_id!r} unknown to repo "
                "(call create_session first or initialize() to scan)"
            )

        slot = self._path / "sessions" / session_id
        async with self._commit_lock:
            await asyncio.to_thread(slot.mkdir, parents=True, exist_ok=True)
            staged_paths: list[str] = []

            # Write files.
            for rel, content in (files or {}).items():
                _validate_relative_path(rel)
                target = slot / rel
                await asyncio.to_thread(
                    target.parent.mkdir, parents=True, exist_ok=True
                )
                if isinstance(content, bytes):
                    await asyncio.to_thread(target.write_bytes, content)
                else:
                    await asyncio.to_thread(
                        target.write_text, content, encoding="utf-8"
                    )
                staged_paths.append(self._repo_relative(target))

            # Stage them.
            if staged_paths:
                await self._run_git("add", "--", *staged_paths)

            # Delete files if requested. ``--ignore-unmatch`` keeps the
            # call idempotent: callers can request a delete without
            # checking whether the file is currently present.
            if delete_files:
                rm_paths: list[str] = []
                for rel in delete_files:
                    _validate_relative_path(rel)
                    rm_paths.append(self._repo_relative(slot / rel))
                await self._run_git(
                    "rm", "--quiet", "--ignore-unmatch", "--", *rm_paths
                )

            # Build the commit message.
            message = _build_message(
                subject=summary,
                workspace_id=self._workspace_id,
                session_id=session_id,
                agent_id=agent_id,
                op=op,
                tool=tool,
                call_id=call_id,
            )

            # Commit. ``--allow-empty`` keeps trailer-only commits
            # (e.g. ``status_change`` with no file changes) viable.
            await self._run_git(
                "-c",
                f"user.name={_AUTHOR_NAME}",
                "-c",
                f"user.email={_AUTHOR_EMAIL}",
                "commit",
                "--allow-empty",
                "--quiet",
                "-m",
                message,
            )

            sha_raw, _ = await self._run_git("rev-parse", "HEAD")
            sha = sha_raw.strip()
            logger.debug(
                "LocalStateRepo committed",
                extra={
                    "sha": sha,
                    "session_id": session_id,
                    "agent_id": agent_id,
                    "op": op,
                    "tool": tool,
                    "call_id": call_id,
                },
            )
            return sha

    async def commit_arbitrary(
        self,
        *,
        summary: str,
        files: dict[str, str | bytes] | None = None,
        delete_files: list[str] | None = None,
        trailers: dict[str, str] | None = None,
    ) -> str:
        """Commit arbitrary files relative to the ``.state/`` repo root.

        Used by callers (like the graph executor) that don't fit the
        session-scoped :meth:`commit` shape. File paths in ``files``
        and ``delete_files`` are relative to ``.state/`` (e.g.
        ``"graphs/gs-1/state.json"``).

        ``trailers`` is a free-form ``key -> value`` mapping appended
        to the commit message; the standard
        ``X-Primer-Workspace: <id>`` trailer is added automatically.

        Acquires the same commit lock as :meth:`commit` so concurrent
        graph + agent commits serialise safely on ``.git/index.lock``.
        Returns the new commit's full SHA.
        """
        async with self._commit_lock:
            staged_paths: list[str] = []
            for rel, content in (files or {}).items():
                _validate_relative_path(rel)
                target = self._path / rel
                await asyncio.to_thread(
                    target.parent.mkdir, parents=True, exist_ok=True
                )
                if isinstance(content, bytes):
                    await asyncio.to_thread(target.write_bytes, content)
                else:
                    await asyncio.to_thread(
                        target.write_text, content, encoding="utf-8"
                    )
                staged_paths.append(self._repo_relative(target))

            if staged_paths:
                await self._run_git("add", "--", *staged_paths)

            if delete_files:
                rm_paths: list[str] = []
                for rel in delete_files:
                    _validate_relative_path(rel)
                    rm_paths.append(self._repo_relative(self._path / rel))
                await self._run_git(
                    "rm", "--quiet", "--ignore-unmatch", "--", *rm_paths
                )

            # Build commit message: subject + workspace trailer + caller trailers.
            message_lines = [
                summary,
                "",
                f"{_TRAILER_WORKSPACE}: {self._workspace_id}",
            ]
            for key, value in (trailers or {}).items():
                message_lines.append(f"{key}: {value}")
            message = "\n".join(message_lines)

            await self._run_git(
                "-c",
                f"user.name={_AUTHOR_NAME}",
                "-c",
                f"user.email={_AUTHOR_EMAIL}",
                "commit",
                "--allow-empty",
                "--quiet",
                "-m",
                message,
            )
            sha_raw, _ = await self._run_git("rev-parse", "HEAD")
            return sha_raw.strip()

    async def history(
        self,
        *,
        session_id: str | None = None,
        agent_id: str | None = None,
        limit: int = 100,
    ) -> list[CommitInfo]:
        """Return commits, optionally filtered. Newest first.

        Filters are AND-ed when both supplied. Implemented by passing
        ``--grep=<trailer-pattern>`` to ``git log`` so the filter
        happens inside git rather than after the fact.
        """
        if limit < 1:
            raise ValueError("limit must be >= 1")

        args = [
            "log",
            f"--max-count={limit}",
            "--format=%H%x1f%s%x1f%cI%x1f%(trailers:only,unfold)%x1e",
        ]
        if session_id is not None:
            args += ["--grep", f"^{_TRAILER_SESSION}: {session_id}$"]
        if agent_id is not None:
            args += ["--grep", f"^{_TRAILER_AGENT}: {agent_id}$"]
        # AND-match only matters when both --grep flags are present;
        # git treats single --grep without --all-match as a single
        # pattern, which is what we want.
        if session_id is not None and agent_id is not None:
            args.append("--all-match")

        stdout, _ = await self._run_git(*args, allow_empty_repo=True)
        return _parse_log_records(stdout)

    async def show_commit(self, sha: str) -> dict:
        """Return ``{subject, body, parent, files: [{path, status, patch}]}``
        for a single commit, ready to render as a diff view.

        ``status`` is git's name-status code (``A``/``M``/``D``/``R``…).
        ``patch`` is the unified diff payload for that file (text only —
        binary files are surfaced with a ``<binary>`` placeholder so the
        endpoint doesn't 500 on non-text blobs).
        """
        if not sha:
            raise ValueError("sha must be non-empty")
        # 1) Header: subject + parent.
        try:
            head_out, _ = await self._run_git(
                "show",
                "--no-patch",
                "--format=%P%x1f%s%x1f%b",
                sha,
            )
        except _GitCommandError as exc:
            stderr = exc.stderr.lower()
            if "unknown revision" in stderr or "bad revision" in stderr:
                raise FileNotFoundError(f"commit {sha!r} not found") from exc
            raise
        header = head_out.strip("\n")
        parent = subject = body = ""
        if header:
            parts = header.split("\x1f")
            if len(parts) >= 1:
                parent = parts[0].split(" ")[0] if parts[0] else ""
            if len(parts) >= 2:
                subject = parts[1]
            if len(parts) >= 3:
                body = "\x1f".join(parts[2:])
        # 2) Per-file status + patch.
        try:
            ns_out, _ = await self._run_git(
                "diff-tree",
                "--no-commit-id",
                "-r",
                "--name-status",
                sha,
            )
        except _GitCommandError:
            ns_out = ""
        files: list[dict] = []
        for line in ns_out.splitlines():
            line = line.rstrip()
            if not line:
                continue
            try:
                status, path = line.split("\t", 1)
            except ValueError:
                continue
            files.append({"path": path, "status": status, "patch": ""})
        # 3) Pull the patch and slot it onto each file entry.
        try:
            patch_out, _ = await self._run_git(
                "show",
                "--format=",
                "--no-color",
                sha,
            )
        except _GitCommandError:
            patch_out = ""
        # Split the unified diff by file boundaries (``diff --git`` lines).
        # Each block lists the +++/--- header and the patch hunks.
        cur_path: str | None = None
        cur: list[str] = []
        path_to_patch: dict[str, str] = {}
        for raw in patch_out.splitlines():
            if raw.startswith("diff --git"):
                if cur_path is not None:
                    path_to_patch[cur_path] = "\n".join(cur)
                cur = [raw]
                # diff --git a/<path> b/<path>
                try:
                    cur_path = raw.split(" b/", 1)[1].strip()
                except IndexError:
                    cur_path = None
            else:
                cur.append(raw)
        if cur_path is not None:
            path_to_patch[cur_path] = "\n".join(cur)
        for f in files:
            f["patch"] = path_to_patch.get(f["path"], "")
        return {
            "sha": sha,
            "subject": subject,
            "body": body.strip("\n"),
            "parent": parent or None,
            "files": files,
        }

    async def read_at(self, sha: str, path: str) -> bytes:
        """Read a file from a historical commit.

        ``path`` is relative to the repo root (e.g.
        ``"sessions/sess-1/messages.jsonl"``). Returns the raw bytes.
        Raises :class:`FileNotFoundError` if the path doesn't exist at
        that commit.
        """
        if not sha:
            raise ValueError("sha must be non-empty")
        # ``git show`` writes the blob to stdout. We capture as bytes
        # because callers may store binary blobs.
        try:
            stdout_bytes, _ = await self._run_git_bytes("show", f"{sha}:{path}")
        except _GitCommandError as exc:
            stderr = exc.stderr.lower()
            if "exists on disk, but not in" in stderr or "does not exist" in stderr or "not found" in stderr:
                raise FileNotFoundError(f"{path!r} not in {sha}") from exc
            raise
        return stdout_bytes

    async def load_session_info(self, session_id: str) -> SessionInfo | None:
        """Read ``sessions/<session_id>/session.json`` if present."""
        _validate_session_id(session_id)
        path = self._path / "sessions" / session_id / "session.json"
        if not await asyncio.to_thread(path.exists):
            return None
        raw = await asyncio.to_thread(path.read_bytes)
        return SessionInfo.model_validate_json(raw)

    async def load_agent_binding(self, session_id: str) -> AgentBinding | None:
        """Read ``sessions/<session_id>/agent.json`` if present."""
        _validate_session_id(session_id)
        path = self._path / "sessions" / session_id / "agent.json"
        if not await asyncio.to_thread(path.exists):
            return None
        raw = await asyncio.to_thread(path.read_bytes)
        return AgentBinding.model_validate_json(raw)

    async def load_waiting_state(self, session_id: str) -> WaitingState | None:
        """Read ``sessions/<session_id>/waiting.json`` if present.

        Returns ``None`` when the file is missing -- this is the normal
        case for sessions not in WAITING. The caller (typically
        :meth:`AgentSession.waiting_state`) decides what to do when the
        file is absent vs. when it's present but ill-formed (the
        latter raises a Pydantic ``ValidationError``).
        """
        _validate_session_id(session_id)
        path = self._path / "sessions" / session_id / "waiting.json"
        if not await asyncio.to_thread(path.exists):
            return None
        raw = await asyncio.to_thread(path.read_bytes)
        return _waiting_state_adapter.validate_json(raw)

    async def read_state_file(self, path: str) -> bytes | None:
        """Read a file by path relative to the ``.state/`` repo root.

        Returns the file bytes, or ``None`` if the file is absent.
        ``path`` must be a relative forward-slash path (e.g.
        ``"sessions/sess-1/messages.jsonl"``).
        """
        _validate_relative_path(path)
        target = self._path / path
        if not await asyncio.to_thread(target.exists):
            return None
        return await asyncio.to_thread(target.read_bytes)

    # ---- internals -------------------------------------------------------

    def _repo_relative(self, p: Path) -> str:
        """Return ``p`` as a forward-slash path relative to the repo root."""
        rel = p.resolve().relative_to(self._path.resolve())
        return rel.as_posix()

    async def _scan_existing_sessions(self) -> None:
        """Rebuild the session_id -> agent_id cache from disk."""
        sessions_dir = self._path / "sessions"
        if not await asyncio.to_thread(sessions_dir.exists):
            return
        for entry in await asyncio.to_thread(_list_subdirs, sessions_dir):
            agent_path = entry / "agent.json"
            if not await asyncio.to_thread(agent_path.exists):
                continue
            raw = await asyncio.to_thread(agent_path.read_bytes)
            try:
                binding = AgentBinding.model_validate_json(raw)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "LocalStateRepo: skipping unreadable agent.json",
                    extra={"path": str(agent_path), "error": str(exc)},
                )
                continue
            self._agent_by_session[entry.name] = binding.agent_id

    async def _run_git(
        self,
        *args: str,
        allow_empty_repo: bool = False,
    ) -> tuple[str, str]:
        """Run ``git -C <path> <args...>`` and return (stdout, stderr) as text."""
        stdout_bytes, stderr_bytes = await self._run_git_bytes(
            *args, allow_empty_repo=allow_empty_repo
        )
        return (
            stdout_bytes.decode("utf-8", errors="replace"),
            stderr_bytes.decode("utf-8", errors="replace"),
        )

    async def _run_git_bytes(
        self,
        *args: str,
        allow_empty_repo: bool = False,
    ) -> tuple[bytes, bytes]:
        """Run ``git -C <path> <args...>`` and return raw bytes for stdout.

        Kills the subprocess and raises :class:`SubprocessTimeoutError` if it
        does not complete within ``self._subprocess_timeout_seconds``.
        """
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(self._path),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=self._subprocess_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            raise SubprocessTimeoutError(
                f"git {' '.join(args)} timed out after "
                f"{self._subprocess_timeout_seconds}s"
            ) from exc
        if proc.returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="replace")
            # ``git log`` on a brand-new repo with no commits exits 128
            # with "does not have any commits yet". Treat as empty.
            if (
                allow_empty_repo
                and proc.returncode == 128
                and "does not have any commits yet" in stderr_text
            ):
                return b"", stderr
            raise _GitCommandError(args, proc.returncode or -1, stderr_text)
        return stdout, stderr


# ===========================================================================
# Helpers
# ===========================================================================


class _GitCommandError(RuntimeError):
    """Raised when a git subprocess exits non-zero."""

    def __init__(self, args: tuple[str, ...], code: int, stderr: str) -> None:
        self.args_used = args
        self.returncode = code
        self.stderr = stderr
        super().__init__(
            f"git {' '.join(args)} exited {code}: {stderr.strip()}"
        )


def _validate_session_id(session_id: str) -> None:
    """Reject session ids that would let writes escape the slot."""
    if not session_id:
        raise ValueError("session_id must be non-empty")
    if "/" in session_id or "\\" in session_id or session_id in (".", ".."):
        raise ValueError(f"session_id contains illegal characters: {session_id!r}")
    if "\x00" in session_id:
        raise ValueError("session_id contains a null byte")


def _validate_relative_path(rel: str) -> None:
    """Reject paths that would escape the session slot."""
    if not rel:
        raise ValueError("path must be non-empty")
    if rel.startswith("/") or rel.startswith("\\"):
        raise ValueError(f"path must be relative: {rel!r}")
    parts = Path(rel).parts
    if any(part == ".." for part in parts):
        raise ValueError(f"path must not contain '..': {rel!r}")
    if "\x00" in rel:
        raise ValueError("path contains a null byte")


def _build_message(
    *,
    subject: str,
    workspace_id: str,
    session_id: str,
    agent_id: str,
    op: str,
    tool: str | None,
    call_id: str | None,
) -> str:
    """Build a commit message with trailers in the order the spec dictates."""
    trailers = [
        f"{_TRAILER_WORKSPACE}: {workspace_id}",
        f"{_TRAILER_SESSION}: {session_id}",
        f"{_TRAILER_AGENT}: {agent_id}",
        f"{_TRAILER_OP}: {op}",
    ]
    if tool is not None:
        trailers.append(f"{_TRAILER_TOOL}: {tool}")
    if call_id is not None:
        trailers.append(f"{_TRAILER_CALL}: {call_id}")
    return f"{subject}\n\n" + "\n".join(trailers) + "\n"


_RECORD_SEP = "\x1e"
_FIELD_SEP = "\x1f"


def _parse_log_records(stdout: str) -> list[CommitInfo]:
    """Parse the output of our ``git log --format=...`` invocation."""
    out: list[CommitInfo] = []
    for record in stdout.split(_RECORD_SEP):
        record = record.strip("\n")
        if not record:
            continue
        parts = record.split(_FIELD_SEP)
        if len(parts) < 4:
            continue
        sha, subject, committed_at_iso, trailer_block = parts[0], parts[1], parts[2], parts[3]
        trailers = _parse_trailers(trailer_block)
        out.append(
            CommitInfo(
                sha=sha,
                subject=subject,
                committed_at=datetime.fromisoformat(committed_at_iso),
                workspace_id=trailers.get(_TRAILER_WORKSPACE),
                session_id=trailers.get(_TRAILER_SESSION),
                agent_id=trailers.get(_TRAILER_AGENT),
                op=trailers.get(_TRAILER_OP),
                tool=trailers.get(_TRAILER_TOOL),
                call_id=trailers.get(_TRAILER_CALL),
            )
        )
    return out


def _parse_trailers(block: str) -> dict[str, str]:
    """Parse ``Key: value`` lines from a trailer block."""
    result: dict[str, str] = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        result[key.strip()] = value.strip()
    return result


def _list_subdirs(root: Path) -> list[Path]:
    return [entry for entry in root.iterdir() if entry.is_dir()]


__all__ = [
    "LocalStateRepo",
]
