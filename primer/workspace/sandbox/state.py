"""SandboxStateRepo -- runtime-backed state repo inside a Sandbox.

Delegates every state operation to the workspace runtime via the
:class:`WSSandbox` thin passthroughs ``state_commit`` / ``state_read`` /
``state_history`` (which in turn delegate to :class:`RuntimeClient`).

Runtime protocol requirement: the connected runtime must report protocol
version >= 1.1.  If it reports an older version, ``create_session``,
``commit``, and ``commit_arbitrary`` raise a :class:`ValidationError`
so the API layer returns HTTP 422 instead of letting callers hit an
``EUNSUPPORTED`` from the runtime.

File layout and commit message format are byte-compatible with
:class:`primer.workspace.local.state.LocalStateRepo` so the conformance
suite (Task 4.1) can compare the two implementations.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import TypeAdapter

from primer.model.except_ import ValidationError
from primer.model.workspace import CommitInfo, Op
from primer.model.workspace_session import (
    AgentBinding,
    SessionInfo,
    WaitingState,
)
from primer.session.mutation_lock import KeyedLock
from primer.workspace.state_helpers import (
    TRAILER_AGENT as _TRAILER_AGENT,
    TRAILER_CALL as _TRAILER_CALL,
    TRAILER_OP as _TRAILER_OP,
    TRAILER_SESSION as _TRAILER_SESSION,
    TRAILER_TOOL as _TRAILER_TOOL,
    TRAILER_WORKSPACE as _TRAILER_WORKSPACE,
    VALID_OPS as _VALID_OPS,
    build_message as _build_message,
    validate_relative_path as _validate_relative_path,
    validate_session_id as _validate_session_id,
)

if TYPE_CHECKING:
    from primer.model.chat import Message


logger = logging.getLogger(__name__)


# Trailer keys, the valid-op set, the commit-message builder, and the path /
# session-id validators are shared with LocalStateRepo via
# :mod:`primer.workspace.state_helpers` (imported above under the same private
# names so call sites stay unchanged; the message format MUST stay
# byte-compatible with the local backend for the conformance suite).

_waiting_state_adapter: TypeAdapter[WaitingState] = TypeAdapter(WaitingState)


# ---------------------------------------------------------------------------
# Minimal structural protocol for a state-capable sandbox
# ---------------------------------------------------------------------------


@runtime_checkable
class _StateCapableSandbox(Protocol):
    """Structural protocol: a Sandbox that also exposes runtime state ops.

    :class:`WSSandbox` satisfies this protocol once the passthroughs are
    defined.  Tests can inject any mock that provides these attributes.
    """

    @property
    def protocol_version(self) -> str: ...

    async def state_commit(
        self,
        *,
        files: dict[str, bytes],
        deletes: list[str],
        message: str,
        allow_empty: bool = False,
    ) -> str: ...

    async def state_read(self, paths: list[str]) -> dict[str, bytes | None]: ...

    async def state_history(
        self,
        *,
        session_id: str | None = None,
        agent_id: str | None = None,
        limit: int = 50,
    ) -> list[dict]: ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_version(version_str: str) -> tuple[int, int]:
    """Parse ``"MAJOR.MINOR"`` into ``(major, minor)`` integers.

    Returns ``(0, 0)`` for any string that does not conform to the
    ``major.minor`` pattern so that comparisons degrade gracefully.
    """
    try:
        parts = version_str.split(".", 1)
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
        return (major, minor)
    except (ValueError, IndexError):
        return (0, 0)


def _build_arbitrary_message(
    *,
    subject: str,
    workspace_id: str,
    trailers: dict[str, str] | None,
) -> str:
    """Build a commit message for :meth:`SandboxStateRepo.commit_arbitrary`."""
    lines = [
        subject,
        "",
        f"{_TRAILER_WORKSPACE}: {workspace_id}",
    ]
    for key, value in (trailers or {}).items():
        lines.append(f"{key}: {value}")
    return "\n".join(lines)


def _map_commit_dict(raw: dict) -> CommitInfo:
    """Map a raw commit dict (from state_history) to :class:`CommitInfo`.

    Handles two wire shapes:

    * **Trailer-nested** -- the runtime emits trailer key/value pairs under a
      ``"trailers"`` sub-dict keyed by ``"X-Primer-*"`` header names.  This
      was the originally planned shape.
    * **Flat** -- the runtime ops.py ``_parse_log_records`` function extracts
      the trailer values directly into top-level fields
      (``"workspace_id"``, ``"session_id"``, ``"agent_id"``, ``"op"``,
      ``"tool"``, ``"call_id"``).  This is the actual shape the real runtime
      returns, so we must handle it first.

    The flat shape takes priority; the nested shape acts as a fallback.
    """
    ts_raw = raw.get("committed_at") or raw.get("timestamp") or raw.get("ts")
    if isinstance(ts_raw, (int, float)):
        committed_at = datetime.fromtimestamp(float(ts_raw), tz=timezone.utc)
    elif isinstance(ts_raw, str):
        try:
            committed_at = datetime.fromisoformat(ts_raw)
        except ValueError:
            committed_at = datetime.now(tz=timezone.utc)
    else:
        committed_at = datetime.now(tz=timezone.utc)

    # Prefer the flat fields the runtime populates directly.
    workspace_id: str | None = raw.get("workspace_id") or None
    session_id: str | None = raw.get("session_id") or None
    agent_id: str | None = raw.get("agent_id") or None
    op: str | None = raw.get("op") or None
    tool: str | None = raw.get("tool") or None
    call_id: str | None = raw.get("call_id") or None

    # Fall back to nested trailers dict when the flat fields are absent.
    raw_trailers = raw.get("trailers") or {}
    if raw_trailers:
        trailers: dict[str, str] = {k: str(v) for k, v in raw_trailers.items()}
        if workspace_id is None:
            workspace_id = trailers.get(_TRAILER_WORKSPACE)
        if session_id is None:
            session_id = trailers.get(_TRAILER_SESSION)
        if agent_id is None:
            agent_id = trailers.get(_TRAILER_AGENT)
        if op is None:
            op = trailers.get(_TRAILER_OP)
        if tool is None:
            tool = trailers.get(_TRAILER_TOOL)
        if call_id is None:
            call_id = trailers.get(_TRAILER_CALL)

    return CommitInfo(
        sha=raw.get("sha", ""),
        subject=raw.get("subject", ""),
        committed_at=committed_at,
        workspace_id=workspace_id,
        session_id=session_id,
        agent_id=agent_id,
        op=op,
        tool=tool,
        call_id=call_id,
    )


# ---------------------------------------------------------------------------
# SandboxStateRepo
# ---------------------------------------------------------------------------


class SandboxStateRepo:
    """Runtime-backed state repo -- full StateRepo protocol via state ops.

    Every git operation is dispatched as a ``state_commit`` / ``state_read``
    / ``state_history`` RPC to the in-container runtime server over the
    existing WebSocket connection.  No shell access is required.

    Protocol requirement: the connected runtime must report protocol
    version >= 1.1 (introduced the state ops).  On older runtimes the
    mutating methods raise :class:`ValidationError` with a clear message.
    """

    def __init__(
        self,
        sandbox: object,
        *,
        state_path: str,
        workspace_id: str,
    ) -> None:
        if not workspace_id:
            raise ValueError("workspace_id must be non-empty")
        self._sandbox = sandbox
        self._state_path = state_path
        self._workspace_id = workspace_id
        self._commit_lock = asyncio.Lock()
        # Serialises the messages.jsonl read->rewrite window (session
        # instruction appends, the executor's turn persist) against the
        # append-file event-row writes (``Workspace.append_message_line``).
        # Distinct from ``_commit_lock`` (which the rewrite still takes
        # internally) to avoid re-entrancy. Keyed by session id so unrelated
        # sessions never contend. Mirrors LocalStateRepo._messages_locks.
        self._messages_locks = KeyedLock()
        # session_id -> agent_id cache (populated by create_session /
        # initialize scan; used by commit() to resolve the agent trailer).
        self._agent_by_session: dict[str, str] = {}
        # Steer-deferral state, keyed by session id. Mirrors
        # LocalStateRepo: while a session compacts, an arriving steer is
        # recorded PENDING here and drained (FIFO) after the compaction marker.
        # Always mutated under the caller's messages_lock (the accessors below
        # take no lock of their own -- the non-reentrant messages_lock must not
        # be re-taken).
        self._compaction_flags: dict[str, bool] = {}
        self._pending_steers: dict[str, list["Message"]] = {}

    # ---- steer-deferral state (guarded by the caller's messages_lock) -----

    def begin_compaction(self, session_id: str) -> None:
        """Mark ``session_id`` as compacting. Caller MUST hold messages_lock."""
        self._compaction_flags[session_id] = True

    def end_compaction(self, session_id: str) -> None:
        """Clear the compacting flag. Caller MUST hold messages_lock."""
        self._compaction_flags[session_id] = False

    def is_compacting(self, session_id: str) -> bool:
        """Whether ``session_id`` is mid-compaction. Caller holds messages_lock."""
        return self._compaction_flags.get(session_id, False)

    def add_pending_steer(self, session_id: str, message: "Message") -> None:
        """Record a steer deferred during compaction. Caller holds messages_lock."""
        self._pending_steers.setdefault(session_id, []).append(message)

    def peek_pending_steers(self, session_id: str) -> list["Message"]:
        """Return the pending steers WITHOUT clearing them (FIFO).

        Caller holds messages_lock. Used to drain-after-commit: read the
        queued steers, persist them durably, and only then call
        :meth:`drain_pending_steers`, so a failed write leaves them queued
        instead of dropping them.
        """
        return list(self._pending_steers.get(session_id, []))

    def drain_pending_steers(self, session_id: str) -> list["Message"]:
        """Return + clear the pending steers (FIFO). Caller holds messages_lock."""
        return self._pending_steers.pop(session_id, [])

    @property
    def workspace_id(self) -> str:
        return self._workspace_id

    @property
    def state_path(self) -> str:
        return self._state_path

    def messages_lock(self, session_id: str) -> AbstractAsyncContextManager[None]:
        """Mutual exclusion for one session's messages.jsonl writers.

        Acquired by :meth:`Workspace.append_message_line` and by every
        full-file rewriter across its read->rewrite window, so the two never
        interleave. Keyed by ``session_id`` so unrelated sessions do not
        contend. Mirrors ``LocalStateRepo.messages_lock``.
        """
        return self._messages_locks.acquire(session_id)

    # ------------------------------------------------------------------
    # Version guard
    # ------------------------------------------------------------------

    def _require_state_ops(self) -> None:
        """Raise ValidationError if the runtime is too old for state ops.

        Reads :attr:`WSSandbox.protocol_version` (the server-negotiated
        version captured during the hello handshake).  Any runtime that
        does not expose the state ops will have version < 1.1 and will
        cause a clean 4xx error rather than an obscure runtime error.
        """
        if not isinstance(self._sandbox, _StateCapableSandbox):
            raise ValidationError(
                "The sandbox backend does not support runtime state operations. "
                "Use a WSSandbox-backed SandboxStateRepo (container or k8s workspace)."
            )
        version_str = self._sandbox.protocol_version  # type: ignore[union-attr]
        version = _parse_version(version_str)
        if version < (1, 1):
            raise ValidationError(
                f"workspace runtime image is too old (protocol {version_str!r}); "
                "graph and stateful sessions require runtime protocol >= 1.1; "
                "rebuild the runtime image"
            )

    def _state_sandbox(self) -> _StateCapableSandbox:
        """Return the sandbox cast to :class:`_StateCapableSandbox`.

        Callers MUST call :meth:`_require_state_ops` first.
        """
        return self._sandbox  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # initialize
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Ensure the in-container git state repo exists.

        If the sandbox satisfies :class:`_StateCapableSandbox` (i.e. it
        exposes the runtime state-ops protocol -- ``state_commit``,
        ``state_read``, ``state_history``), the runtime server manages its
        own git repo lifecycle; no host-side git invocation is needed and
        this method is a no-op.

        For exec-only sandboxes that do NOT expose the state ops (e.g. a
        hypothetical thin exec-only shim), we fall back to running
        ``git init`` inside the sandbox via ``exec``.  The operation is
        idempotent: if the ``.git`` directory already exists ``git init``
        is a no-op.
        """
        # Runtime-backed sandboxes (WSSandbox and any _StateCapableSandbox
        # implementation) initialize their own git repo on the server side.
        # Skip the exec-based git init to avoid running host-side git
        # against in-container absolute paths.
        if isinstance(self._sandbox, _StateCapableSandbox):
            return

        exec_fn = getattr(self._sandbox, "exec", None)
        if exec_fn is None:
            # Non-exec sandbox (e.g. unit-test mock); skip git init.
            return

        state_dir = self._state_path  # e.g. /workspace/.state
        # Create the directory and initialise the git repo in one shell
        # invocation.  We configure user.name/user.email locally so that
        # commits never fail due to a missing global git config.
        init_script = (
            f"mkdir -p {state_dir} && "
            f"git -C {state_dir} init --initial-branch=main && "
            f"git -C {state_dir} config user.email 'primer@local' && "
            f"git -C {state_dir} config user.name 'primer'"
        )
        result = await exec_fn(
            ["sh", "-c", init_script],
        )
        if result.exit_code != 0:
            raise RuntimeError(
                f"SandboxStateRepo.initialize: git init failed "
                f"(rc={result.exit_code}): {result.stderr}"
            )

    # ------------------------------------------------------------------
    # create_session
    # ------------------------------------------------------------------

    async def create_session(
        self,
        session_info: SessionInfo,
        agent_binding: AgentBinding,
    ) -> str:
        """Write session.json + agent.json and commit with op ``attach``.

        Returns the SHA of the attach commit.
        """
        self._require_state_ops()
        session_id = session_info.session_id
        _validate_session_id(session_id)

        # Cache agent_id so commit() can resolve trailers.
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
            self._agent_by_session.pop(session_id, None)
            raise

    # ------------------------------------------------------------------
    # delete_session
    # ------------------------------------------------------------------

    async def delete_session(self, session_id: str) -> None:
        """Reap a session's persisted slot from the runtime state repo.

        Removes ``sessions/<session_id>/session.json``, ``agent.json`` and
        ``waiting.json`` via a state-op commit (a ``git rm`` inside the pod).
        Dropping ``session.json`` / ``agent.json`` is sufficient to make
        :meth:`SandboxWorkspace._rehydrate_locked` skip the session on the
        next :meth:`SandboxWorkspace.list_sessions`: ``load_session_info`` /
        ``load_agent_binding`` then read the (now absent) files and return
        ``None``.

        Uses the state ops -- not an ``exec rm`` or file ``delete`` -- on
        purpose: the slot lives in the runtime-managed working tree that
        ``state_read`` consults, so a ``git rm`` commit removes exactly the
        file ``state_read`` would otherwise return. A raw filesystem delete
        of a separately-computed path could miss (or, in FakeSandbox, never
        touch) the tracked copy. Idempotent: git rm uses
        ``--ignore-unmatch`` and the commit is ``--allow-empty``, so
        deleting an already-gone slot is a harmless no-op.

        Best-effort at the caller: exceptions propagate so the workspace
        layer can log-and-continue when the workspace is unreachable.
        """
        self._require_state_ops()
        _validate_session_id(session_id)
        await self.commit_arbitrary(
            summary=f"{session_id}: detach",
            delete_files=[
                f"sessions/{session_id}/session.json",
                f"sessions/{session_id}/agent.json",
                f"sessions/{session_id}/waiting.json",
            ],
            trailers={_TRAILER_SESSION: session_id},
        )
        # Drop the agent-id cache entry so a stale binding can't linger.
        self._agent_by_session.pop(session_id, None)

    # ------------------------------------------------------------------
    # commit
    # ------------------------------------------------------------------

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

        Returns the new commit SHA. Acquires ``_commit_lock`` for the
        duration so concurrent commits are serialised.
        """
        self._require_state_ops()
        _validate_session_id(session_id)
        if op not in _VALID_OPS:
            raise ValueError(f"unknown op: {op!r}")

        agent_id = self._agent_by_session.get(session_id)
        if agent_id is None:
            raise LookupError(
                f"session {session_id!r} unknown to repo "
                "(call create_session first)"
            )

        # Build the file map with paths relative to the state repo root.
        commit_files: dict[str, bytes] = {}
        for rel, content in (files or {}).items():
            _validate_relative_path(rel)
            path = f"sessions/{session_id}/{rel}"
            if isinstance(content, str):
                commit_files[path] = content.encode("utf-8")
            else:
                commit_files[path] = content

        deletes: list[str] = []
        for rel in (delete_files or []):
            _validate_relative_path(rel)
            deletes.append(f"sessions/{session_id}/{rel}")

        message = _build_message(
            subject=summary,
            workspace_id=self._workspace_id,
            session_id=session_id,
            agent_id=agent_id,
            op=op,
            tool=tool,
            call_id=call_id,
        )

        sandbox = self._state_sandbox()
        async with self._commit_lock:
            sha = await sandbox.state_commit(
                files=commit_files,
                deletes=deletes,
                message=message,
                allow_empty=True,
            )
        logger.debug(
            "SandboxStateRepo committed",
            extra={
                "sha": sha,
                "session_id": session_id,
                "agent_id": agent_id,
                "op": op,
            },
        )
        return sha

    # ------------------------------------------------------------------
    # commit_arbitrary
    # ------------------------------------------------------------------

    async def commit_arbitrary(
        self,
        *,
        summary: str,
        files: dict[str, str | bytes] | None = None,
        delete_files: list[str] | None = None,
        trailers: dict[str, str] | None = None,
    ) -> str:
        """Commit arbitrary files relative to the state repo root.

        Returns the new commit SHA. Acquires ``_commit_lock`` for the
        duration so concurrent commits are serialised.
        """
        self._require_state_ops()

        commit_files: dict[str, bytes] = {}
        for rel, content in (files or {}).items():
            _validate_relative_path(rel)
            if isinstance(content, str):
                commit_files[rel] = content.encode("utf-8")
            else:
                commit_files[rel] = content

        deletes: list[str] = []
        for rel in (delete_files or []):
            _validate_relative_path(rel)
            deletes.append(rel)

        message = _build_arbitrary_message(
            subject=summary,
            workspace_id=self._workspace_id,
            trailers=trailers,
        )

        sandbox = self._state_sandbox()
        async with self._commit_lock:
            sha = await sandbox.state_commit(
                files=commit_files,
                deletes=deletes,
                message=message,
                allow_empty=True,
            )
        return sha

    # ------------------------------------------------------------------
    # history
    # ------------------------------------------------------------------

    async def history(
        self,
        *,
        session_id: str | None = None,
        agent_id: str | None = None,
        limit: int = 100,
    ) -> list[CommitInfo]:
        """Return commits, optionally filtered by session or agent. Newest first."""
        if not isinstance(self._sandbox, _StateCapableSandbox):
            return []
        sandbox = self._state_sandbox()
        raw_commits = await sandbox.state_history(
            session_id=session_id,
            agent_id=agent_id,
            limit=limit,
        )
        return [_map_commit_dict(c) for c in raw_commits]

    # ------------------------------------------------------------------
    # show_commit
    # ------------------------------------------------------------------

    async def show_commit(self, sha: str) -> dict:
        """Not supported on the sandbox backend.

        Raises :class:`NotImplementedError` so the API layer returns HTTP 501.
        """
        raise NotImplementedError(
            "show_commit is not supported on the sandbox/container/k8s backend"
        )

    # ------------------------------------------------------------------
    # load helpers
    # ------------------------------------------------------------------

    async def load_session_info(self, session_id: str) -> SessionInfo | None:
        """Read ``sessions/<session_id>/session.json`` if present."""
        _validate_session_id(session_id)
        if not isinstance(self._sandbox, _StateCapableSandbox):
            return None
        sandbox = self._state_sandbox()
        result = await sandbox.state_read([f"sessions/{session_id}/session.json"])
        raw = result.get(f"sessions/{session_id}/session.json")
        if raw is None:
            return None
        info = SessionInfo.model_validate_json(raw)
        # Populate cache if not already present.
        self._agent_by_session.setdefault(session_id, info.agent_id)
        return info

    async def load_agent_binding(self, session_id: str) -> AgentBinding | None:
        """Read ``sessions/<session_id>/agent.json`` if present."""
        _validate_session_id(session_id)
        if not isinstance(self._sandbox, _StateCapableSandbox):
            return None
        sandbox = self._state_sandbox()
        result = await sandbox.state_read([f"sessions/{session_id}/agent.json"])
        raw = result.get(f"sessions/{session_id}/agent.json")
        if raw is None:
            return None
        binding = AgentBinding.model_validate_json(raw)
        self._agent_by_session.setdefault(session_id, binding.agent_id)
        return binding

    async def list_session_ids(self) -> list[str]:
        """Enumerate every session id persisted in the runtime state repo.

        The ``.state`` tree is a runtime-managed git repo, so the canonical
        way to discover sessions without raw filesystem access is the
        ``state_history`` op: every session's ``attach`` commit (and every
        subsequent turn commit) carries a ``Session:`` trailer. We collect
        the distinct, non-empty session ids across the commit log.

        This is the enumeration source :meth:`SandboxWorkspace.get_session`
        and :meth:`SandboxWorkspace.list_sessions` use to rehydrate handles
        that were created in another process (the API/worker split) or
        before a platform restart -- the parallel to
        :meth:`LocalStateRepo._scan_existing_sessions` on the local backend
        (which scans ``sessions/`` on disk). Returns an empty list on an
        exec-only sandbox that does not expose the state ops.
        """
        if not isinstance(self._sandbox, _StateCapableSandbox):
            return []
        sandbox = self._state_sandbox()
        # A high limit so we don't miss sessions whose only commit is the
        # initial attach buried under many turn commits from busier
        # siblings. The history is bounded by workspace lifetime; the
        # runtime returns newest-first.
        raw_commits = await sandbox.state_history(limit=10_000)
        seen: dict[str, None] = {}
        for raw in raw_commits:
            info = _map_commit_dict(raw)
            sid = info.session_id
            if sid and sid not in seen:
                seen[sid] = None
                self._agent_by_session.setdefault(sid, info.agent_id or "")
        return list(seen)

    async def load_waiting_state(self, session_id: str) -> WaitingState | None:
        """Read ``sessions/<session_id>/waiting.json`` if present."""
        _validate_session_id(session_id)
        if not isinstance(self._sandbox, _StateCapableSandbox):
            return None
        sandbox = self._state_sandbox()
        result = await sandbox.state_read([f"sessions/{session_id}/waiting.json"])
        raw = result.get(f"sessions/{session_id}/waiting.json")
        if raw is None:
            return None
        return _waiting_state_adapter.validate_json(raw)

    async def read_state_file(self, path: str) -> bytes | None:
        """Read a file by path relative to the state repo root.

        Returns the file bytes, or ``None`` if the file is absent.
        """
        _validate_relative_path(path)
        if not isinstance(self._sandbox, _StateCapableSandbox):
            return None
        sandbox = self._state_sandbox()
        result = await sandbox.state_read([path])
        return result.get(path)


__all__ = ["SandboxStateRepo"]
