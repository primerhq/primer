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
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from matrix.agent.base import _BaseAgentExecutor
from matrix.agent.compaction import CompactionStrategy
from matrix.agent.tool_manager import ToolExecutionManager
from matrix.model.chat import Message
from matrix.model.except_ import ConflictError
from matrix.model.workspace_session import (
    SessionStatus,
    _UserInputWaiting,
)


if TYPE_CHECKING:
    from matrix.int.llm import LLM
    from matrix.model.agent import Agent
    from matrix.model.provider import LLMModel
    from matrix.workspace.session import AgentSession


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
    ) -> None:
        # Extend the agent's system prompt with the workspace fragment
        # so the LLM sees workspace-tool documentation in its system
        # context. This produces a *new* Agent instance with the
        # combined prompt; the original agent definition is unchanged.
        from matrix.model.agent import Agent as _Agent

        composite_system_prompt = list(agent.system_prompt) + [
            session.system_prompt_fragment
        ]
        composite_agent = _Agent(
            id=agent.id,
            description=agent.description,
            model=agent.model,
            temperature=agent.temperature,
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
        """Append ``turn_messages`` to ``messages.jsonl`` via ``commit_state``."""
        new_text = await self._appended_jsonl(turn_messages)
        await self._session.commit_state(
            summary=f"{self._session.session_id}: assistant turn",
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
    ) -> None:
        """Rewrite ``messages.jsonl`` with the compacted history.

        Git history naturally preserves the pre-compaction snapshot --
        anyone inspecting ``.state/sessions/<id>/`` can ``git show``
        the previous commit to recover the original messages.
        """
        new_jsonl = (
            "\n".join(m.model_dump_json() for m in compacted) + "\n"
            if compacted
            else ""
        )
        await self._session.commit_state(
            summary=f"{self._session.session_id}: compaction",
            op="message",
            files={"messages.jsonl": new_jsonl},
        )

    # ---- Public surface override (drives session status) -----------------

    async def invoke(self, messages, *, response_format=None):
        """Drive the session through one user-driven turn."""
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

    def _state_path(self) -> Path:
        """Return the path to this session's slot under ``.state/sessions/``."""
        return (
            self._session._state.path  # type: ignore[attr-defined]
            / "sessions"
            / self._session.session_id
        )

    def _messages_jsonl_path(self) -> Path:
        return self._state_path() / "messages.jsonl"

    async def _read_messages_jsonl(self) -> list[Message]:
        path = self._messages_jsonl_path()

        def _read() -> list[Message]:
            if not path.exists():
                return []
            out: list[Message] = []
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.rstrip("\n")
                    if not line:
                        continue
                    out.append(Message.model_validate_json(line))
            return out

        return await asyncio.to_thread(_read)

    async def _appended_jsonl(self, new_messages: list[Message]) -> str:
        existing = await self._read_messages_jsonl_text()
        if existing and not existing.endswith("\n"):
            existing += "\n"
        return existing + "\n".join(m.model_dump_json() for m in new_messages) + "\n"

    async def _read_messages_jsonl_text(self) -> str:
        path = self._messages_jsonl_path()

        def _read() -> str:
            if not path.exists():
                return ""
            return path.read_text(encoding="utf-8")

        return await asyncio.to_thread(_read)

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


def _ends_with_question(text: str) -> bool:
    """Crude heuristic: does the trimmed text end with a question mark?"""
    stripped = text.rstrip()
    return stripped.endswith("?")


def _extract_question(text: str) -> str:
    """Return the trimmed text up to and including the trailing question mark."""
    return text.rstrip()


__all__ = ["WorkspaceAgentExecutor"]
