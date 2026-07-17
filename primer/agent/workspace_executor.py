"""Drive an :class:`Agent` over an :class:`AgentSession` in a workspace.

The :class:`WorkspaceAgentExecutor` is the workspace-backed executor.
State persists through :meth:`AgentSession.commit_state` (one git
commit per turn). The agent's tool list is composed from
:attr:`Agent.tools` PLUS :attr:`AgentSession.workspace_tools`, and
the agent's system prompt is extended with
:attr:`AgentSession.system_prompt_fragment`.

Drives :class:`SessionStatus` transitions between turns and at
terminal events (RUNNING <-> WAITING <-> ENDED) based on the LLM's
``Done.stop_reason`` and the pattern of tool calls.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from primer.agent.base import _BaseAgentExecutor
from primer.agent.compaction import CompactionStrategy
from primer.agent.tool_manager import ToolExecutionManager
from primer.model.chat import Message
from primer.model.except_ import ConflictError
from primer.model.graph import build_execution_context
from primer.model.workspace_session import (
    SessionMessageKind,
    SessionMessageRecord,
    SessionStatus,
    _UserInputWaiting,
)
from primer.model.yield_ import YieldToWorker


if TYPE_CHECKING:
    from primer.int.llm import LLM
    from primer.model.agent import Agent
    from primer.model.principal import PrincipalRef
    from primer.model.provider import LLMModel
    from primer.workspace.session import AgentSession


logger = logging.getLogger(__name__)


class WorkspaceAgentExecutor(_BaseAgentExecutor):
    """Workspace-backed executor.

    Constructed against a live :class:`AgentSession`. The
    ``tool_manager`` MUST already include the session's workspace
    tools (typically built via
    :meth:`ToolExecutionManager.for_workspace`).
    """

    def __init__(
        self,
        *,
        agent: "Agent",
        llm: "LLM",
        llm_model: "LLMModel",
        tool_manager: ToolExecutionManager,
        session: "AgentSession",
        compaction: CompactionStrategy | None = None,
        principal: str | None = None,
        identity: "PrincipalRef | None" = None,
    ) -> None:
        # Extend the agent's system prompt with the workspace fragment
        # so the LLM sees workspace-tool documentation in its system
        # context. This produces a *new* Agent instance with the
        # combined prompt; the original agent definition is unchanged.
        from primer.model.agent import Agent as _Agent

        composite_system_prompt = list(agent.system_prompt) + [
            session.system_prompt_fragment
        ]
        composite_agent = _Agent(
            id=agent.id,
            description=agent.description,
            model=agent.model,
            temperature=agent.temperature,
            max_output_tokens=agent.max_output_tokens,
            tools=list(agent.tools),
            system_prompt=composite_system_prompt,
            compaction_prompt=list(agent.compaction_prompt),
        )
        super().__init__(
            agent=composite_agent,
            llm=llm,
            llm_model=llm_model,
            tool_manager=tool_manager,
            compaction=compaction,
            principal=principal,
        )
        self._session = session
        self._execution_context = build_execution_context(
            surface="workspace",
            workspace_id=session.workspace_id,
            session_id=session.session_id,
            principal=principal,
            identity=identity,
        )
        # Trailing :class:`Done.stop_reason` from the most recent
        # :meth:`invoke` call. ``None`` until the first invoke completes.
        # The worker pool's post-turn status mapper reads this to decide
        # whether the session should re-enqueue (``RUNNING``) or wait
        # for inspection (``WAITING``) when the executor exits without
        # having explicitly set the session status itself.
        self.last_done_reason: str | None = None

    @property
    def session(self) -> "AgentSession":
        return self._session

    # ---- Subclass hooks --------------------------------------------------

    async def _load_history(self) -> list[Message]:
        """Read every message from the session's ``messages.jsonl``."""
        return await self._read_messages_jsonl()

    async def _persist_turn(self, turn_messages: list[Message]) -> None:
        """Append ``turn_messages`` to ``messages.jsonl`` via ``commit_state``.

        This is itself a read-modify-rewrite: ``_appended_jsonl`` reads the
        current file and ``commit_state`` writes the whole thing back. The
        session's messages lock is held across BOTH so a concurrent writer
        (an ``append_instruction`` steer, or a streamed event row) cannot
        land in the read->rewrite gap and be silently truncated by the
        rewrite (see arch-review batch 1, MEDIUM-1).

        Lock order is ``messages_lock -> _commit_lock`` (commit_state takes
        the latter internally), matching every other acquirer, and nothing
        inside this critical section appends to messages.jsonl, so the
        non-reentrant lock cannot be re-taken by this task.
        """
        async with self._session.messages_lock:
            new_text = await self._appended_jsonl(turn_messages)
            excerpt = _summary_excerpt_from_messages(turn_messages)
            sid_short = self._session.session_id[-12:]
            if excerpt:
                subject = f"turn[{sid_short}]: {excerpt}"
            else:
                subject = f"turn[{sid_short}]: assistant turn"
            await self._session.commit_state(
                summary=subject,
                op="message",
                files={"messages.jsonl": new_text},
            )

    async def inject_resume_messages(
        self, messages: list[Message],
    ) -> None:
        """Append ``messages`` to the session's history without driving
        a new turn.

        The worker pool's resume path calls this with the
        [assistant_message_with_tool_use, tool_result_message] pair
        rehydrated from ParkedState. After persistence the worker
        clears the park columns and re-enqueues the session;
        a subsequent normal claim picks up and runs the next LLM
        turn against the augmented history.

        Thin wrapper around ``_persist_turn`` so the worker doesn't
        have to reach into a protected method. No status mutation —
        the worker drives the session through the scheduler API.
        """
        if not messages:
            return
        await self._persist_turn(messages)

    async def _replace_compacted_head(
        self,
        compacted: list[Message],
        *,
        summary_message: "Message | None" = None,
        tokens_before: int = 0,
        tokens_after: int = 0,
    ) -> None:
        """Record a compaction by APPENDING one ``compaction_marker`` record.

        ``messages.jsonl`` is APPEND-ONLY: this NEVER whole-file-replaces the
        file (the old behaviour, which wiped the event log and every
        pre-compaction message from the live file). Instead it appends ONE
        ``SessionMessageRecord`` of kind ``COMPACTION_MARKER`` whose payload
        mirrors the chat surface (primer/chat/executor.py). On the NEXT load,
        the two history readers (:meth:`_read_messages_jsonl` and
        :meth:`AgentSession.take_pending_messages`, both via
        :func:`reconstruct_compacted_history`) fold every Message line
        physically before the LAST marker into one synthetic assistant
        summary. The in-memory compacted history for THIS turn is already
        applied by the base loop; this hook only records the marker for future
        loads. Nothing is deleted.

        ``summary_message is None`` => pruning-only compaction (tier 1): there
        is nothing to summarise, so NO marker is written -- the (unpruned)
        Message lines stay live on disk and pruning is re-derived in-memory
        each turn. Writing an empty-summary marker would instead drop the
        pruned head entirely on the next load.

        The messages lock is held across the read+append so it cannot
        interleave with a concurrent O_APPEND event row or another full-file
        rewriter. The lock is NOT held across the compaction LLM call (that
        already completed before this hook runs).
        """
        del compacted  # the marker carries the summary text, not the message list
        if summary_message is None:
            return
        summary_text = "".join(
            part.text
            for part in summary_message.parts
            if getattr(part, "type", None) == "text"
        ).strip()
        if not summary_text:
            return
        async with self._session.messages_lock:
            existing = await self._read_messages_jsonl_text()
            boundary_seq = _max_event_log_seq(existing)
            now = datetime.now(timezone.utc)
            marker = SessionMessageRecord(
                seq=boundary_seq + 1,
                kind=SessionMessageKind.COMPACTION_MARKER,
                payload={
                    "summary": summary_text,
                    "replaced_from_seq": 1,
                    # Message lines are seqless, so physical position in the
                    # append-only file IS the boundary; ``replaced_to_seq`` is
                    # the last event-log seq at/before the marker, recorded so
                    # tap/UI consumers know the seq-space boundary too.
                    "replaced_to_seq": boundary_seq,
                    "model": self._model.name,
                    "tokens_before": tokens_before,
                    "tokens_after": tokens_after,
                    "created_at": now.isoformat(),
                },
                created_at=now,
            )
            if existing and not existing.endswith("\n"):
                existing += "\n"
            new_jsonl = existing + marker.model_dump_json() + "\n"
            await self._session.commit_state(
                summary=f"{self._session.session_id}: compaction marker",
                op="message",
                files={"messages.jsonl": new_jsonl},
            )

    async def _open_compaction_window(self) -> list["Message"]:
        """Set the ``compacting`` flag AND snapshot history atomically.

        Both happen under the SAME per-session messages lock so a steer can
        never land between the snapshot and the flag: any steer either lands
        BEFORE (included in the snapshot, correctly summarised) or AFTER (sees
        the flag, is deferred and drained past the marker). The lock is
        released here -- it is NEVER held across the compaction LLM await.
        Returns the marker-aware history snapshot the base loop compacts.
        """
        async with self._session.messages_lock:
            self._session._state.begin_compaction(self._session.session_id)
            return await self._read_messages_jsonl()

    async def _close_compaction_window(self) -> None:
        """Clear the ``compacting`` flag AND drain deferred steers.

        Held under the messages lock so the flag-clear + drain + append are
        atomic vs a concurrent :meth:`AgentSession.append_instruction`. Drained
        steers are appended (FIFO submission order) AFTER the compaction marker
        that :meth:`_replace_compacted_head` wrote earlier this turn, so the
        next load reads ``[summary(from marker), tail, steer...]``. The flag is
        cleared BEFORE the append so a failure there cannot strand the session
        in a permanently-compacting state.
        """
        async with self._session.messages_lock:
            self._session._state.end_compaction(self._session.session_id)
            pending = self._session._state.drain_pending_steers(
                self._session.session_id
            )
            if not pending:
                return
            new_jsonl = await self._appended_jsonl(pending)
            await self._session.commit_state(
                summary=(
                    f"{self._session.session_id}: apply "
                    f"{len(pending)} deferred steer(s)"
                ),
                op="user_instruction",
                files={"messages.jsonl": new_jsonl},
            )
            for _ in pending:
                logger.info(
                    "session %s: applied a steer deferred during compaction",
                    self._session.session_id,
                )

    async def _ensure_artifact_dir(self) -> None:
        """Best-effort create ``<workspace_root>/artifacts/<session_id>/``.

        Local repos only (sandbox repos have no local path and rely on
        create-on-write via the workspace write tool). Never fatal.
        """
        root = self._session.workspace_root
        if root is None:
            return
        artifact_dir = root / "artifacts" / self._session.session_id
        try:
            await asyncio.to_thread(
                artifact_dir.mkdir, parents=True, exist_ok=True
            )
        except OSError as exc:  # noqa: BLE001 -- best-effort, never fatal
            logger.warning(
                "session %s: best-effort artifact dir create failed: %s",
                self._session.session_id,
                exc,
            )

    # ---- Public surface override (drives session status) -----------------

    async def invoke(self, messages, *, response_format=None):
        """Drive the session through one user-driven turn."""
        await self._ensure_artifact_dir()
        # Pre-turn boundary checks against the session's lifecycle.
        status = await self._session.status()
        if status == SessionStatus.ENDED:
            raise ConflictError(
                f"cannot invoke ENDED session {self._session.session_id!r}"
            )
        if self._session.end_requested:
            await self._session.set_status(
                SessionStatus.ENDED,
                ended_reason="cancelled",
            )
            return
        if self._session.pause_requested:
            await self._session.set_status(SessionStatus.PAUSED)
            return
        if status == SessionStatus.PAUSED:
            await self._session.set_status(SessionStatus.RUNNING)
        elif status == SessionStatus.WAITING:
            # User responded; resume.
            await self._session.set_status(SessionStatus.RUNNING)

        last_done_reason: str | None = None
        # Reset the cached attribute up-front so a stale value from a
        # previous invoke can't leak into the post-turn status mapper.
        self.last_done_reason = None
        try:
            async for ev in super().invoke(
                messages, response_format=response_format
            ):
                if ev.type == "done":
                    last_done_reason = ev.stop_reason  # type: ignore[union-attr]
                yield ev
        except YieldToWorker:
            # A park (tool approval, ask_user, subscribe_to_trigger,
            # watch_files, sleep) is NOT a failure: the base executor raises
            # YieldToWorker to hand the turn back to the worker, which parks
            # the session row for resume. Let it propagate WITHOUT marking the
            # on-disk session slot ENDED/failed -- otherwise the slot is killed
            # at the gate and the resuming claim (especially a cross-process
            # worker that rehydrates the slot) hits "cannot commit state on
            # ENDED session" on inject_resume_messages (see FINDINGS F10/F10c).
            raise
        except Exception:
            try:
                await self._session.set_status(
                    SessionStatus.ENDED,
                    ended_reason="failed",
                )
            except Exception:  # noqa: BLE001 -- best-effort
                logger.warning(
                    "WorkspaceAgentExecutor: failed to set ENDED on error",
                    extra={"session_id": self._session.session_id},
                )
            raise

        # Publish the trailing stop reason so the worker pool's
        # post-turn status mapper can read it without re-iterating.
        self.last_done_reason = last_done_reason

        # Post-turn status transition.
        if last_done_reason == "tool_use":
            return  # inner loop handled tool dispatch already
        if last_done_reason == "error":
            await self._session.set_status(
                SessionStatus.ENDED,
                ended_reason="failed",
            )
            return

        # Heuristic: if the assistant's final text ends with a question
        # mark, treat it as a user-input wait. Crude but workable for
        # v1; a structured ``ask_user`` tool replaces this in a future
        # spec.
        last_assistant_text = await self._fetch_last_assistant_text()
        if last_assistant_text and _ends_with_question(last_assistant_text):
            await self._session.set_status(
                SessionStatus.WAITING,
                waiting_state=_UserInputWaiting(
                    prompt=_extract_question(last_assistant_text),
                    queued_at=datetime.now(timezone.utc),
                ),
            )
            return
        # Otherwise the session stays RUNNING -- the next invoke will
        # continue the conversation.

    # ---- Internals -------------------------------------------------------

    def _messages_jsonl_rel(self) -> str:
        """Return the ``messages.jsonl`` path relative to the state repo root.

        Forward-slash, ``.state``-relative (e.g.
        ``"sessions/sess-1/messages.jsonl"``) -- the contract of
        :meth:`StateRepo.read_state_file`, which both the local and sandbox
        (container/k8s) backends implement. Using the protocol read instead
        of a direct ``self._session._state.path`` filesystem access is what
        lets agent sessions run on sandbox backends, whose state lives in the
        workspace pod and exposes no local ``.path`` (see FINDINGS F-K8S-AGENT).
        """
        return f"sessions/{self._session.session_id}/messages.jsonl"

    async def _read_messages_jsonl(self) -> list[Message]:
        """Rebuild the LLM history, honoring the last compaction marker.

        messages.jsonl is shared between the LLM conversation history
        (role/parts Messages, written by AgentSession + ``_persist_turn``) and
        the session event log (seq/kind/ts SessionMessageRecords, written by
        the dispatch WorkspaceMessageWriter for WS replay). Only the
        Message-shaped lines are LLM history; the event records are skipped
        (see FINDINGS F10b). When a ``compaction_marker`` record is present,
        every Message line physically at/before the LAST marker is folded into
        one synthetic assistant summary (see
        :func:`reconstruct_compacted_history`).
        """
        raw = await self._session._state.read_state_file(self._messages_jsonl_rel())
        if not raw:
            return []
        text = raw.decode("utf-8")

        def _parse() -> list[Message]:
            # Deferred import: reconstruct_compacted_history lives in
            # primer.workspace.session; a module-top import would pull the
            # primer.workspace package initialisation into primer.agent's
            # import graph. Imported here (off the event loop, in the thread).
            from primer.workspace.session import reconstruct_compacted_history

            return reconstruct_compacted_history(text.splitlines())

        return await asyncio.to_thread(_parse)

    async def _appended_jsonl(self, new_messages: list[Message]) -> str:
        existing = await self._read_messages_jsonl_text()
        if existing and not existing.endswith("\n"):
            existing += "\n"
        return existing + "\n".join(m.model_dump_json() for m in new_messages) + "\n"

    async def _read_messages_jsonl_text(self) -> str:
        raw = await self._session._state.read_state_file(self._messages_jsonl_rel())
        if not raw:
            return ""
        return raw.decode("utf-8")

    async def _fetch_last_assistant_text(self) -> str | None:
        """Return the text of the most recent assistant message, or None."""
        msgs = await self._read_messages_jsonl()
        for msg in reversed(msgs):
            if msg.role == "assistant":
                texts: list[str] = []
                for part in msg.parts:
                    if part.type == "text":
                        texts.append(part.text)  # type: ignore[union-attr]
                return "".join(texts) if texts else None
        return None


def _max_event_log_seq(text: str) -> int:
    """Return the highest ``seq`` among event-log records in ``text``, else 0.

    Only :class:`SessionMessageRecord` lines carry a ``seq`` (the interleaved
    role/parts Message lines are seqless). Used to give an appended compaction
    marker a strictly-greater monotonic seq than every prior event-log record.
    """
    max_seq = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(obj, dict)
            and "kind" in obj
            and isinstance(obj.get("seq"), int)
            and obj["seq"] > max_seq
        ):
            max_seq = obj["seq"]
    return max_seq


def _ends_with_question(text: str) -> bool:
    """Crude heuristic: does the trimmed text end with a question mark?"""
    stripped = text.rstrip()
    return stripped.endswith("?")


def _extract_question(text: str) -> str:
    """Return the trimmed text up to and including the trailing question mark."""
    return text.rstrip()


_SUMMARY_MAX_CHARS = 72


def _summary_excerpt_from_messages(messages: "list[Message]") -> str | None:
    """Build a single-line excerpt from the turn's assistant text + any
    tool-call surface, so the commit subject in ``git log`` is actually
    informative.

    Picks the LAST assistant message in ``messages`` and joins any text
    parts; trims to ``_SUMMARY_MAX_CHARS`` characters with an ellipsis.
    Falls back to listing the tool names called when there's no text
    (pure tool-use turn). Returns ``None`` when neither is available.
    """
    last_assistant = None
    for m in messages:
        if getattr(m, "role", None) == "assistant":
            last_assistant = m
    if last_assistant is None:
        return None

    text_parts: list[str] = []
    tool_names: list[str] = []
    for part in last_assistant.parts:
        t = getattr(part, "type", None) or part.__class__.__name__.lower()
        text = getattr(part, "text", None)
        if isinstance(text, str) and text.strip():
            text_parts.append(text.strip())
        # ToolCallPart-shaped: surface the tool name when there's no text.
        tool_name = getattr(part, "tool_name", None) or getattr(part, "name", None)
        if tool_name and "tool" in (t or "").lower():
            tool_names.append(str(tool_name))

    summary: str
    if text_parts:
        summary = " ".join(text_parts).replace("\n", " ").strip()
    elif tool_names:
        summary = "tool_use: " + ", ".join(tool_names)
    else:
        return None

    summary = " ".join(summary.split())
    if len(summary) > _SUMMARY_MAX_CHARS:
        summary = summary[: _SUMMARY_MAX_CHARS - 1].rstrip() + "…"
    return summary


__all__ = ["WorkspaceAgentExecutor"]
