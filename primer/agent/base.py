"""Shared base class for agent executors.

The :class:`_BaseAgentExecutor` is intentionally non-public (leading
underscore in the name); concrete executors are
:class:`primer.agent.AgentExecutor` (chat threads) and
:class:`primer.agent.WorkspaceAgentExecutor` (workspace-backed).

The base class owns:

* The inner LLM loop -- stream events, buffer, dispatch tool calls,
  re-send with tool results, repeat.
* End-of-turn persistence -- materialise the assistant
  :class:`Message` from the streamed events via
  :func:`primer.model.chat.output_to_message`, hand off to the
  subclass's ``_persist_turn`` hook.
* Compaction integration -- call
  :meth:`CompactionStrategy.maybe_compact` before each turn; if it
  fired, hand the compacted history to the subclass's
  ``_replace_compacted_head`` hook.
* Streaming-tap fan-out -- :meth:`subscribe` registers a callback
  that receives every :class:`StreamEvent` concurrently with the
  caller's iterator.
* Hard-overflow recovery -- catch a context-overflow
  :class:`BadRequestError` from the LLM, force-compact, retry once.

Subclasses provide three abstract hooks:

* :meth:`_load_history`
* :meth:`_persist_turn`
* :meth:`_replace_compacted_head`
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from primer.agent.compaction import CompactionStrategy
from primer.agent.compaction_mixin import (
    should_compact as _mixin_should_compact,
)
from primer.agent.events import (
    AgentEventSubscriber,
    Subscription,
    _ExecutorToolResult,
)
from primer.agent.prompt_render import render_system_prompt
from primer.agent.tool_manager import ToolExecutionManager
from primer.model.chat import (
    ExtendedEvent,
    Message,
    StreamEvent,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    Usage,
    output_to_message,
)
from primer.model.except_ import (
    AuthRequiredError,
    BadRequestError,
    PrimerError,
)
from primer.model.graph import build_execution_context


if TYPE_CHECKING:
    from primer.int.llm import LLM
    from primer.model.agent import Agent
    from primer.model.provider import LLMModel


logger = logging.getLogger(__name__)


def _is_context_overflow(exc: BadRequestError) -> bool:
    """Heuristic: is a BadRequestError caused by context overflow?

    The four shipped LLM adapters wrap provider exceptions into
    ``BadRequestError`` without a stable error code for context
    overflow specifically. Match common substrings instead.
    """
    msg = (exc.message or "").lower()
    needles = (
        "context length",
        "context_length",
        "context window",
        "maximum context",
        "context limit",
        "max_tokens",
        "too long",
        "input is too long",
        "tokens exceeds",
        "token limit",
        "prompt is too long",
    )
    return any(n in msg for n in needles)


class _BaseAgentExecutor(ABC):
    """Shared LLM loop + compaction + streaming for both executor types."""

    def __init__(
        self,
        *,
        agent: "Agent",
        llm: "LLM",
        llm_model: "LLMModel",
        tool_manager: ToolExecutionManager,
        compaction: CompactionStrategy | None = None,
        principal: str | None = None,
    ) -> None:
        self._agent = agent
        self._llm = llm
        self._model = llm_model
        self._tool_manager = tool_manager
        self._compaction = compaction or CompactionStrategy()
        self._principal = principal
        # Ambient run context exposed to the system prompt as ``ctx``. Base is
        # surface-agnostic -> memory default; subclasses override with the real
        # surface (AgentExecutor -> "chat", WorkspaceAgentExecutor -> "workspace").
        self._execution_context = build_execution_context()
        self._subscribers: dict[str, AgentEventSubscriber] = {}
        self._subscriber_lock = asyncio.Lock()
        self._last_input_tokens: int | None = None

    # ---- Subclass hooks --------------------------------------------------

    @abstractmethod
    async def _load_history(self) -> list[Message]:
        """Return the prior conversation in chronological order."""

    @abstractmethod
    async def _persist_turn(self, turn_messages: list[Message]) -> None:
        """Append the messages produced during one ``invoke`` call."""

    @abstractmethod
    async def _replace_compacted_head(
        self,
        compacted: list[Message],
    ) -> None:
        """Replace the persisted history with the compacted form."""

    # ---- Public surface --------------------------------------------------

    async def invoke(
        self,
        messages: list[Message],
        *,
        response_format: type[BaseModel] | dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Run one user-driven turn (or chain of tool turns) against the LLM.

        Yields every :class:`StreamEvent` produced by the LLM in the
        order it arrives, plus synthetic
        :class:`_ExecutorToolResult` events wrapped in
        :class:`ExtendedEvent` for each tool result fed back to the
        model. The same events are fanned out concurrently to every
        registered tap subscriber.

        End-of-turn persistence runs once the LLM produces a
        non-tool-use stop. Streaming chunks are NOT persisted -- only
        the materialised :class:`Message` is.
        """
        history = await self._load_history()

        compacted = await self._compaction.maybe_compact(
            agent=self._agent,
            llm=self._llm,
            model=self._model,
            history=history,
            new_messages=messages,
            last_known_input_tokens=self._last_input_tokens,
        )
        if compacted is not None:
            await self._replace_compacted_head(compacted.new_messages)
            history = compacted.new_messages
            logger.info(
                "AgentExecutor: compaction fired",
                extra={
                    "agent_id": self._agent.id,
                    "before_tokens": compacted.estimated_tokens_before,
                    "after_tokens": compacted.estimated_tokens_after,
                    "pruned": compacted.pruned_tool_outputs,
                    "head_replaced": compacted.head_messages_replaced,
                },
            )

        try:
            async for ev in self._run_loop(
                history=history,
                new_messages=messages,
                response_format=response_format,
            ):
                yield ev
        except BadRequestError as exc:
            if not _is_context_overflow(exc):
                raise
            logger.warning(
                "AgentExecutor: hard-overflow detected; force-compacting and retrying",
                extra={"agent_id": self._agent.id, "error": str(exc)},
            )
            forced = await self._compaction.force_compact(
                agent=self._agent,
                llm=self._llm,
                model=self._model,
                history=history,
            )
            await self._replace_compacted_head(forced.new_messages)
            history = forced.new_messages
            async for ev in self._run_loop(
                history=history,
                new_messages=messages,
                response_format=response_format,
            ):
                yield ev

    def subscribe(self, subscriber: AgentEventSubscriber) -> Subscription:
        """Register a streaming-tap subscriber. Returns the subscription handle."""
        sub_id = f"sub-{uuid.uuid4().hex[:12]}"
        self._subscribers[sub_id] = subscriber
        return Subscription(subscription_id=sub_id, _executor=self)

    async def unsubscribe(self, subscription: Subscription) -> None:
        async with self._subscriber_lock:
            self._subscribers.pop(subscription.subscription_id, None)

    # ---- Inner loop ------------------------------------------------------

    async def _run_loop(
        self,
        *,
        history: list[Message],
        new_messages: list[Message],
        response_format: type[BaseModel] | dict[str, Any] | None,
    ) -> AsyncIterator[StreamEvent]:
        from primer.agent.loop import run_agent_turn

        full_turn_messages: list[Message] = list(new_messages)
        prompt = self._build_prompt(history, new_messages)

        # Shared helper handles the LLM+tool dispatch loop. We tap
        # every event into our subscriber fan-out + caller stream;
        # the helper writes the assistant + tool-result messages
        # directly into ``full_turn_messages`` for end-of-turn
        # persistence below.
        last_input_tokens_holder: list[int | None] = []
        from primer.model.yield_ import YieldToWorker
        try:
            async for event in run_agent_turn(
                agent=self._agent,
                llm=self._llm,
                llm_model=self._model,
                tool_manager=self._tool_manager,
                prompt=prompt,
                response_format=response_format,
                principal=self._principal,
                messages_out=full_turn_messages,
                last_input_tokens_out=last_input_tokens_holder,
            ):
                await self._emit(event)
                yield event
        except YieldToWorker as exc:
            # The tool engine raised mid-turn. ``full_turn_messages``
            # already contains the assistant message that carried the
            # tool_use (loop.py:143 appends it before dispatch), but
            # ``_persist_turn`` hasn't run yet (we only persist on a
            # clean end-of-stream below). Stamp the delta onto the
            # exception so the worker's park hook can preserve it in
            # the parked_state blob — load-bearing for the resume
            # path's [assistant_tool_use, tool_result] history
            # injection.
            #
            # The slice strips ``new_messages`` (which the executor's
            # caller already has) so the stamp is just what this turn
            # accumulated up to the yield point.
            exc.llm_messages = list(full_turn_messages[len(new_messages):])
            raise

        if last_input_tokens_holder:
            self._last_input_tokens = last_input_tokens_holder[0]

        # Persist only when the loop actually produced an assistant
        # message (helper appends it on the first non-tool stop or
        # not at all on empty/error streams).
        produced_assistant = any(
            m.role == "assistant" for m in full_turn_messages[len(new_messages):]
        )
        if produced_assistant:
            await self._persist_turn(full_turn_messages)

    # ---- Tool dispatch ---------------------------------------------------

    async def _dispatch_tool_calls(
        self,
        calls: list[ToolCallPart],
    ) -> list[Message]:
        """Dispatch tool calls and return the resulting tool-role messages.

        AuthRequiredError is the only exception that propagates --
        subclasses handle it (chat: terminal stream Error; workspace:
        WAITING transition). All other PrimerErrors are converted to
        ToolResultPart(error=True) by the manager itself.
        """
        result_parts: list[ToolResultPart] = []
        for call in calls:
            try:
                rp = await self._tool_manager.execute(
                    call,
                    principal=self._principal,
                )
            except AuthRequiredError:
                raise
            except PrimerError as exc:  # defence-in-depth.
                rp = ToolResultPart(id=call.id, output=str(exc), error=True)
            result_parts.append(rp)

        if not result_parts:
            return []
        return [Message(role="tool", parts=list(result_parts))]

    # ---- Prompt building -------------------------------------------------

    def _build_prompt(
        self,
        history: list[Message],
        new_messages: list[Message],
    ) -> list[Message]:
        """Assemble the full prompt: system + history + new user input."""
        parts: list[Message] = []
        if self._agent.system_prompt:
            sys_text = render_system_prompt(
                self._agent.system_prompt, self._execution_context
            )
            parts.append(
                Message(role="system", parts=[TextPart(text=sys_text)])
            )
        parts.extend(history)
        parts.extend(new_messages)
        return parts

    # ---- Event fan-out ---------------------------------------------------

    async def _emit(self, event: StreamEvent) -> None:
        """Fan ``event`` out to every registered subscriber concurrently."""
        if not self._subscribers:
            return
        async with self._subscriber_lock:
            subs = list(self._subscribers.values())

        async def _safe(sub: AgentEventSubscriber) -> None:
            try:
                await sub.on_event(event)
            except Exception as exc:  # noqa: BLE001 -- subscriber isolation
                logger.warning(
                    "AgentExecutor: subscriber raised; isolating",
                    extra={"error": str(exc)},
                )

        await asyncio.gather(*[_safe(s) for s in subs], return_exceptions=False)


__all__ = ["_BaseAgentExecutor"]
