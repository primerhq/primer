"""Concrete :class:`AgentSession` for the Workspace abstraction.

One execution of one agent on one workspace. Holds the per-session
state handle, mediates writes to the shared :class:`StateRepo` and the
shared :class:`TruncationStore`, validates lifecycle transitions, and
exposes the user-facing surface (``append_instruction`` /
``request_pause`` / ``request_resume`` / ``request_end``) plus the
runtime-facing surface (``commit_state`` / ``cache_output`` /
``set_status`` / ``take_pending_messages``).

Use :meth:`AgentSession.start` to allocate a fresh slot; the workspace
calls this from its ``start_session()`` method. A session created on one
process can be re-attached on another by reconstructing it from its
persisted on-disk slot (session.json + agent.json) via the constructor;
``LocalWorkspace.get_session`` does this so a worker process can run a
session the API process allocated.

See ``docs/superpowers/specs/2026-05-02-workspace-design.md`` (the
"AgentSession" section + "Session lifecycle" section) for the full
design.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Literal

from pydantic import TypeAdapter

from primer.model.chat import Message, TextPart
from primer.model.except_ import ConflictError
from primer.model.workspace_session import (
    AgentBinding,
    Instruction,
    SessionInfo,
    SessionStatus,
    WaitingState,
)


if TYPE_CHECKING:
    from primer.model.workspace import Op
    from primer.workspace.local.cache import LocalTruncationStore
    from primer.workspace.local.state import LocalStateRepo
    from primer.workspace.tool import WorkspaceTool


logger = logging.getLogger(__name__)


# Used by both ``waiting_state()`` and ``set_status()`` for round-tripping
# the discriminated union to / from JSON.
_waiting_state_adapter: TypeAdapter[WaitingState] = TypeAdapter(WaitingState)


# ===========================================================================
# Helpers
# ===========================================================================


_SYSTEM_PROMPT_TEMPLATE = """\
You are running inside a primer Workspace as session `{session_id}`.

You have access to the following workspace tools (file + shell):
ls, read, write, edit, glob, grep, exec.

Your conversation state and persistent notes for THIS session live under
`.state/sessions/{session_id}/`. Every assistant turn is committed to a
git repo at `.state/`. You can inspect your own history with:
  exec("git log -- sessions/{session_id}/", workdir=".state")
  exec("git show <sha>", workdir=".state")

Other sessions may be running concurrently on the same workspace --
you share the filesystem and shell with them. Use `.state/shared/` to
coordinate if you need to.

Large tool outputs are automatically saved to `.tmp/{session_id}/tool_<id>.txt`
and the tool result will give you the path. Use read with offset/limit
or grep to inspect them; do NOT try to dump them with cat.

If you have a todo list, store it at
`.state/sessions/{session_id}/todos.json`. Keep it updated as you
work; history is preserved automatically.

The user can append further instructions to your session at any time.
These appear as new user-role messages between your turns -- treat
them as you would any user message in your conversation.
"""


def _generate_instruction_id() -> str:
    return f"ins-{uuid.uuid4().hex[:16]}"


def _normalise_path(path: str) -> str:
    """Stable canonical form for the read-tracker keys.

    POSIX-flavoured because workspaces present a unified filesystem
    surface to the agent regardless of host OS. Trailing separators
    are collapsed; ``./`` prefixes are dropped; relative segments
    are kept literal so ``a`` and ``./a`` collapse to the same key.
    """
    return PurePosixPath(path.replace("\\", "/")).as_posix()


# ---------------------------------------------------------------------------
# Status transition table
# ---------------------------------------------------------------------------
#
# ENDED is terminal; once entered, no other transitions are allowed.
# WAITING requires a `waiting_state` argument. Every other transition
# is a free move.

_LEGAL_TRANSITIONS: dict[SessionStatus, frozenset[SessionStatus]] = {
    SessionStatus.RUNNING: frozenset(
        {
            SessionStatus.RUNNING,  # idempotent
            SessionStatus.WAITING,
            SessionStatus.PAUSED,
            SessionStatus.ENDED,
        }
    ),
    SessionStatus.WAITING: frozenset(
        {
            SessionStatus.RUNNING,
            SessionStatus.WAITING,  # idempotent (e.g. re-arm with new waiting_state)
            SessionStatus.PAUSED,
            SessionStatus.ENDED,
        }
    ),
    SessionStatus.PAUSED: frozenset(
        {
            SessionStatus.RUNNING,
            SessionStatus.WAITING,
            SessionStatus.PAUSED,  # idempotent
            SessionStatus.ENDED,
        }
    ),
    SessionStatus.ENDED: frozenset({SessionStatus.ENDED}),  # terminal
}


# ===========================================================================
# AgentSession
# ===========================================================================


class AgentSession:
    """One execution of one agent on one workspace.

    Created by :meth:`AgentSession.start` (or directly via the
    constructor for tests / restoring an existing slot). The constructor
    is cheap; ``start`` writes the initial slot to the state repo.

    Concurrency: every state mutation is wrapped in this session's
    ``_lock``. The :class:`StateRepo`'s commit lock further serialises
    writes across sibling sessions sharing the same repo, so callers do
    not need to synchronise externally.
    """

    def __init__(
        self,
        *,
        session_info: SessionInfo,
        agent_binding: AgentBinding,
        state_repo: "LocalStateRepo",
        truncation_store: "LocalTruncationStore",
        workspace_tools: "list[WorkspaceTool] | tuple[WorkspaceTool, ...]" = (),
    ) -> None:
        if session_info.agent_id != agent_binding.agent_id:
            raise ValueError(
                "session_info.agent_id and agent_binding.agent_id disagree"
            )
        self._info = session_info
        self._binding = agent_binding
        self._state = state_repo
        self._cache = truncation_store
        self._tools: list["WorkspaceTool"] = list(workspace_tools)
        self._lock = asyncio.Lock()
        self._pause_requested = False
        self._end_requested = False
        # Read-tracker for the write tool's read-before-write rule.
        # Populated by the read tool via :meth:`mark_read`; consulted by
        # the write tool via :meth:`was_read`. Volatile (in-memory only).
        self._read_files: set[str] = set()

    # ---- Construction / lifecycle ----------------------------------------

    @classmethod
    async def start(
        cls,
        *,
        session_id: str,
        workspace_id: str,
        agent_binding: AgentBinding,
        state_repo: "LocalStateRepo",
        truncation_store: "LocalTruncationStore",
        workspace_tools: "list[WorkspaceTool] | tuple[WorkspaceTool, ...]" = (),
        instructions: str | None = None,
        parent_session_id: str | None = None,
    ) -> "AgentSession":
        """Allocate a fresh slot and return a live session handle.

        Writes ``session.json`` and ``agent.json`` via
        :meth:`StateRepo.create_session` (one ``attach`` commit), then
        appends the ``instructions`` (if any) as the first user-role
        message in ``messages.jsonl`` (one ``user_instruction`` commit
        from :meth:`append_instruction`).

        The new session starts in :attr:`SessionStatus.RUNNING`.
        """
        now = datetime.now(timezone.utc)
        info = SessionInfo(
            session_id=session_id,
            agent_id=agent_binding.agent_id,
            workspace_id=workspace_id,
            status=SessionStatus.RUNNING,
            started_at=now,
            last_activity_at=now,
            initial_instructions=instructions,
            parent_session_id=parent_session_id,
        )
        await state_repo.create_session(info, agent_binding)
        session = cls(
            session_info=info,
            agent_binding=agent_binding,
            state_repo=state_repo,
            truncation_store=truncation_store,
            workspace_tools=workspace_tools,
        )
        if instructions:
            await session.append_instruction(instructions)
        return session

    # ---- Identity properties ---------------------------------------------

    @property
    def session_id(self) -> str:
        return self._info.session_id

    @property
    def agent_id(self) -> str:
        return self._binding.agent_id

    @property
    def workspace_id(self) -> str:
        return self._info.workspace_id

    @property
    def workspace_tools(self) -> list["WorkspaceTool"]:
        """Snapshot of the workspace tool set for this session."""
        return list(self._tools)

    @property
    def system_prompt_fragment(self) -> str:
        """Markdown injected into the agent's system prompt at session start."""
        return _SYSTEM_PROMPT_TEMPLATE.format(session_id=self.session_id)

    # ---- Pause / end flag accessors (set by request_*; read by runtime) --

    @property
    def pause_requested(self) -> bool:
        return self._pause_requested

    @property
    def end_requested(self) -> bool:
        return self._end_requested

    # ---- Runtime-facing per-turn surface ---------------------------------

    async def commit_state(
        self,
        *,
        summary: str,
        op: "Op",
        tool: str | None = None,
        call_id: str | None = None,
        files: dict[str, str | bytes] | None = None,
    ) -> str:
        """Commit one turn's writes to this session's slot. Returns SHA."""
        if self._info.status == SessionStatus.ENDED:
            raise ConflictError(
                f"cannot commit state on ENDED session {self.session_id!r}"
            )
        sha = await self._state.commit(
            self.session_id,
            summary=summary,
            op=op,
            tool=tool,
            call_id=call_id,
            files=files,
        )
        self._touch_last_activity()
        return sha

    async def cache_output(self, text: str) -> str:
        """Write ``text`` to this session's tmp cache; return absolute path."""
        path = await self._cache.write(text, session_id=self.session_id)
        return str(path)

    # ---- Read-tracker (in-memory; consumed by the write tool) ------------

    def mark_read(self, path: str) -> None:
        """Record that the agent read ``path`` during this session.

        Called by the ``read`` workspace tool. The ``write`` tool
        consults :meth:`was_read` to enforce the read-before-write rule
        unless the caller passes ``force=True``.
        """
        self._read_files.add(_normalise_path(path))

    def was_read(self, path: str) -> bool:
        """Whether the agent has read ``path`` in this session."""
        return _normalise_path(path) in self._read_files

    # ---- User-facing operations ------------------------------------------

    async def status(self) -> SessionStatus:
        """Current lifecycle state. Cheap; backed by in-memory cache."""
        return self._info.status

    async def waiting_state(self) -> WaitingState | None:
        """If status is WAITING, return what the session is waiting on.

        Returns ``None`` when status is anything else; consults
        ``waiting.json`` on disk when status is WAITING (so a runtime
        that updated waiting.json without going through this object
        is still seen).
        """
        if self._info.status != SessionStatus.WAITING:
            return None
        return await self._state.load_waiting_state(self.session_id)

    async def info(self) -> SessionInfo:
        """Full :class:`SessionInfo` (timestamps, ended_reason, etc.)."""
        return self._info

    async def refresh_from_disk(self) -> None:
        """Re-sync the in-memory :class:`SessionInfo` from ``session.json``.

        The cached ``_info`` is a snapshot taken when this handle was built;
        when the session's turn ran through a DIFFERENT handle (a worker
        process, or a worker-mode workspace cache distinct from this one),
        the authoritative terminal status is committed to ``session.json``
        on shared disk but this handle's ``_info`` is never updated. Callers
        that read status across the process / cache boundary
        (``LocalWorkspace.get_session`` / ``list_sessions``) invoke this so
        the read reflects disk.

        No-op once this handle is already ENDED (terminal is immutable) and
        when disk has no slot. Only ADVANCES the cached status toward the
        on-disk one; it never rewinds a locally-set terminal state.
        """
        if self._info.status == SessionStatus.ENDED:
            return
        disk = await self._state.load_session_info(self.session_id)
        if disk is None:
            return
        # Adopt disk only when it is at/ahead of the cached view. The only
        # cross-boundary staleness we need to heal is "disk ENDED, cache
        # still RUNNING/WAITING"; adopting a terminal disk view is always
        # safe and is the case the workspace tools depend on.
        if disk.status == SessionStatus.ENDED:
            async with self._lock:
                if self._info.status != SessionStatus.ENDED:
                    self._info = disk

    async def append_instruction(self, content: str) -> Instruction:
        """Queue a user instruction for delivery to the next turn.

        Allowed in any non-ENDED status. In WAITING, the runtime
        typically observes the new message and transitions back to
        RUNNING (clearing ``waiting.json``); the transition itself is
        the runtime's responsibility, not this method's.

        Returns the :class:`Instruction` record (id + timestamps) for
        caller bookkeeping.
        """
        if not content:
            raise ValueError("instruction content must be non-empty")
        if self._info.status == SessionStatus.ENDED:
            raise ConflictError(
                f"cannot append instruction to ENDED session {self.session_id!r}"
            )

        async with self._lock:
            now = datetime.now(timezone.utc)
            instruction = Instruction(
                instruction_id=_generate_instruction_id(),
                session_id=self.session_id,
                content=content,
                queued_at=now,
            )
            message = Message(role="user", parts=[TextPart(text=content)])
            new_messages_jsonl = await self._appended_messages_jsonl(message)

            # Updated session metadata: last_activity_at moves forward.
            updated_info = self._info.model_copy(
                update={"last_activity_at": now},
            )
            sid_short = self.session_id[-12:]
            excerpt = " ".join(content.split())
            if len(excerpt) > 60:
                excerpt = excerpt[:59].rstrip() + "…"
            await self._state.commit(
                self.session_id,
                summary=f"user[{sid_short}]: {excerpt}" if excerpt else f"user[{sid_short}]: user_instruction",
                op="user_instruction",
                files={
                    "messages.jsonl": new_messages_jsonl,
                    "session.json": updated_info.model_dump_json(indent=2),
                },
            )
            self._info = updated_info
            return instruction

    async def request_pause(self, *, reason: str | None = None) -> None:
        """Set the pause flag the runtime checks before each turn.

        Idempotent. Raises :class:`ConflictError` if the session is
        already ENDED. The actual status transition to PAUSED happens
        when the runtime observes the flag at the next turn boundary
        and calls :meth:`set_status`.
        """
        del reason  # captured by callers for logging; not persisted here
        if self._info.status == SessionStatus.ENDED:
            raise ConflictError(
                f"cannot pause ENDED session {self.session_id!r}"
            )
        self._pause_requested = True

    async def request_resume(self) -> None:
        """Clear the pause flag.

        Idempotent. Raises :class:`ConflictError` if the session is
        ENDED. The runtime moves PAUSED back to RUNNING (or WAITING if
        ``waiting.json`` is still present) at its next opportunity.
        """
        if self._info.status == SessionStatus.ENDED:
            raise ConflictError(
                f"cannot resume ENDED session {self.session_id!r}"
            )
        self._pause_requested = False

    async def request_end(
        self,
        *,
        reason: Literal["cancelled"] = "cancelled",
    ) -> None:
        """Set the terminal flag. Idempotent.

        The runtime aborts the in-flight turn (if any) and calls
        :meth:`set_status` with ``ENDED`` and the supplied
        ``ended_reason``. If a runtime isn't driving this session
        (e.g. a teardown path), call :meth:`aclose` for the same
        effect plus tmp cleanup.
        """
        del reason  # the runtime reads it from the request when transitioning
        self._end_requested = True

    # ---- Runtime-only mutators -------------------------------------------

    async def set_status(
        self,
        status: SessionStatus,
        *,
        ended_reason: Literal["completed", "failed", "cancelled"] | None = None,
        waiting_state: WaitingState | None = None,
    ) -> None:
        """Persist a status transition. Called by the agent runtime.

        Validates the transition. When entering WAITING, ``waiting_state``
        MUST be supplied -- it's serialised to ``waiting.json`` in the
        same commit. When leaving WAITING, ``waiting.json`` is deleted
        in the same commit. When entering ENDED, ``ended_reason`` MUST
        be supplied; the per-session tmp subdirectory is reaped.
        """
        async with self._lock:
            current = self._info.status
            allowed = _LEGAL_TRANSITIONS.get(current, frozenset())
            if status not in allowed:
                raise ConflictError(
                    f"illegal transition {current.value!r} -> {status.value!r}"
                )
            if status == SessionStatus.WAITING and waiting_state is None:
                raise ConflictError(
                    "transition to WAITING requires a waiting_state"
                )
            if status == SessionStatus.ENDED and ended_reason is None:
                raise ConflictError(
                    "transition to ENDED requires an ended_reason"
                )

            now = datetime.now(timezone.utc)
            updates: dict[str, object] = {
                "status": status,
                "last_activity_at": now,
            }
            if status == SessionStatus.ENDED:
                updates["ended_reason"] = ended_reason
                updates["ended_at"] = now
            updated_info = self._info.model_copy(update=updates)

            files: dict[str, str | bytes] = {
                "session.json": updated_info.model_dump_json(indent=2),
            }
            delete_files: list[str] = []
            if status == SessionStatus.WAITING:
                files["waiting.json"] = _waiting_state_adapter.dump_json(
                    waiting_state, indent=2
                ).decode("utf-8")
            if (
                current == SessionStatus.WAITING
                and status != SessionStatus.WAITING
            ):
                delete_files.append("waiting.json")

            sid_short = self.session_id[-12:]
            await self._state.commit(
                self.session_id,
                summary=f"status[{sid_short}]: -> {status.value}",
                op="status_change",
                files=files,
                delete_files=delete_files or None,
            )
            self._info = updated_info

            if status == SessionStatus.ENDED:
                # Reap the per-session tmp subdir so disk is released
                # immediately rather than waiting for the retention sweep.
                try:
                    await self._cache.cleanup_session(self.session_id)
                except Exception as exc:  # noqa: BLE001 -- cleanup is advisory
                    logger.warning(
                        "AgentSession: tmp cleanup failed on ENDED transition",
                        extra={"session_id": self.session_id, "error": str(exc)},
                    )

    async def take_pending_messages(self) -> list[Message]:
        """Return the messages added since the last assistant turn.

        Reads ``messages.jsonl`` and returns every message after the
        most recent ``role == "assistant"`` entry. If no assistant
        message exists yet (fresh session), returns every message in
        the log -- typically just the initial user instruction.

        Uses the :class:`StateRepo` protocol's ``read_state_file`` so
        this works for both local and sandbox (container/k8s) backends.
        """
        rel = f"sessions/{self.session_id}/messages.jsonl"
        raw: bytes | None = await self._state.read_state_file(rel)

        def _parse(data: bytes) -> list[Message]:
            messages: list[Message] = []
            for line in data.decode("utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                # messages.jsonl interleaves LLM-history Messages
                # (role/parts) with session event-log records
                # (seq/kind/ts) written by the dispatch writer. Keep only
                # the Message-shaped lines (see FINDINGS F10b).
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not (isinstance(obj, dict) and "role" in obj and "parts" in obj):
                    continue
                messages.append(Message.model_validate(obj))
            return messages

        all_messages = _parse(raw) if raw else []
        last_assistant_idx = -1
        for i, msg in enumerate(all_messages):
            if msg.role == "assistant":
                last_assistant_idx = i
        return all_messages[last_assistant_idx + 1 :]

    async def aclose(self) -> None:
        """Release the session handle.

        Equivalent to ``set_status(ENDED, ended_reason="completed")``
        when the session isn't already ended. Idempotent.
        """
        if self._info.status == SessionStatus.ENDED:
            return
        await self.set_status(SessionStatus.ENDED, ended_reason="completed")

    # ---- Internals -------------------------------------------------------

    async def _appended_messages_jsonl(self, message: Message) -> str:
        """Return the new messages.jsonl content with ``message`` appended.

        Uses the :class:`StateRepo` protocol's ``read_state_file`` so
        this works for both local and sandbox (container/k8s) backends.
        """
        rel = f"sessions/{self.session_id}/messages.jsonl"
        raw: bytes | None = await self._state.read_state_file(rel)
        existing = raw.decode("utf-8") if raw else ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        return existing + message.model_dump_json() + "\n"

    def _touch_last_activity(self) -> None:
        """Update last_activity_at on the in-memory SessionInfo only.

        On-disk session.json is not rewritten by this -- callers that
        commit external files have already taken the commit lock and
        included whatever updates they need. This method just keeps the
        in-memory mirror fresh for cheap status() / info() reads.
        """
        self._info = self._info.model_copy(
            update={"last_activity_at": datetime.now(timezone.utc)}
        )


__all__ = [
    "AgentSession",
]
