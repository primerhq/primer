"""Shared single-turn agent loop.

Extracts the inner LLM + tool-dispatch loop from
:class:`_BaseAgentExecutor` so both the agent executor (chat /
workspace) and the graph executor (per-node invocation) share the
same logic. Keeps the project's agent-loop semantics consistent
no matter which entry point invoked the agent.

Behaviour:

* Calls ``llm.stream(...)`` with the supplied prompt, ``response_format``,
  and the tool catalogue from the supplied :class:`ToolExecutionManager`.
* Yields every event live (no buffering at this layer).
* When the assistant emits :class:`ToolCallPart`s, dispatches each via
  the manager, synthesises an :class:`ExtendedEvent(_ExecutorToolResult)`
  for taps, and re-arms the LLM call with the tool-result messages
  appended.
* Loops until the assistant produces a non-tool stop OR the LLM stream
  yields no convertible events (empty / error stream).
* Writes the assistant + tool-result messages into the caller-provided
  ``messages_out`` list (mutated in place).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from primer.agent.tool_manager import ToolExecutionManager
from primer.model.chat import (
    ExtendedEvent,
    Message,
    StreamEvent,
    ToolCallPart,
    ToolResultPart,
    Usage,
    output_to_message,
    _ExecutorToolResult,
)
from primer.model.except_ import AuthRequiredError, MatrixError


if TYPE_CHECKING:
    from primer.int.llm import LLM
    from primer.model.agent import Agent
    from primer.model.provider import LLMModel


logger = logging.getLogger(__name__)


async def run_agent_turn(
    *,
    agent: "Agent",
    llm: "LLM",
    llm_model: "LLMModel",
    tool_manager: ToolExecutionManager,
    prompt: list[Message],
    response_format: dict[str, Any] | None = None,
    principal: str | None = None,
    messages_out: list[Message] | None = None,
    last_input_tokens_out: list[int | None] | None = None,
) -> AsyncIterator[StreamEvent]:
    """Run one full agent turn with tool dispatch; stream events live.

    Parameters
    ----------
    agent
        The agent definition (used for ``temperature``).
    llm, llm_model
        LLM client + model resolved by the caller.
    tool_manager
        Source of the tool catalogue + dispatch surface. Pass an
        empty :class:`ToolExecutionManager` if the agent should run
        without tools.
    prompt
        The full prompt at turn start: typically
        ``[system?, *history, *new_user_messages]``.
    response_format
        Optional JSON Schema (or Pydantic class) forwarded to
        ``llm.stream``.
    principal
        Forwarded to every :meth:`ToolExecutionManager.execute` call
        for OAuth-aware MCP toolsets.
    messages_out
        Optional caller-provided list. The helper appends every
        message produced during the turn (assistant message + tool-
        result messages) to it, in order.
    last_input_tokens_out
        Optional caller-provided single-element list. The helper
        sets ``[0]`` to the most recent ``Usage.input_tokens`` value
        observed during the turn (or leaves it as-is if the LLM
        never emitted Usage).

    Raises
    ------
    matrix.model.except_.AuthRequiredError
        Propagated from a tool dispatch -- callers handle this
        (chat: terminal stream Error; workspace: WAITING transition;
        graph: per-node FAILED).
    """
    tools = await tool_manager.list_tools(principal=principal)

    while True:
        buffered: list[StreamEvent] = []
        stream = llm.stream(
            model=llm_model.name,
            messages=prompt,
            temperature=agent.temperature,
            response_format=response_format,
            tools=tools,
            tool_choice="auto",
        )
        async for event in stream:
            buffered.append(event)
            yield event
            if (
                last_input_tokens_out is not None
                and isinstance(event, Usage)
            ):
                if not last_input_tokens_out:
                    last_input_tokens_out.append(event.input_tokens)
                else:
                    last_input_tokens_out[0] = event.input_tokens

        try:
            assistant_msg = output_to_message(buffered)
        except ValueError as exc:
            # Empty / error-only stream. The events were already emitted to
            # subscribers, so the user sees something, but the orchestrator
            # would otherwise treat the turn as a quiet success and tight-loop
            # the LLM. Log enough to make the situation diagnosable from
            # production logs.
            logger.warning(
                "agent loop: LLM stream produced no assistant message; "
                "ending turn without persisting (event_count=%d, error=%s)",
                len(buffered), exc,
            )
            return

        if messages_out is not None:
            messages_out.append(assistant_msg)

        tool_calls = [
            p for p in assistant_msg.parts if isinstance(p, ToolCallPart)
        ]
        if not tool_calls:
            return

        tool_result_msgs = await _dispatch_tool_calls(
            tool_calls,
            tool_manager=tool_manager,
            principal=principal,
        )
        for trm in tool_result_msgs:
            if messages_out is not None:
                messages_out.append(trm)
            for part in trm.parts:
                if isinstance(part, ToolResultPart):
                    synth = ExtendedEvent(
                        extended=_ExecutorToolResult(
                            call_id=part.id,
                            output=part.output,
                            error=part.error,
                        )
                    )
                    yield synth

        prompt = prompt + [assistant_msg, *tool_result_msgs]


async def _dispatch_tool_calls(
    calls: list[ToolCallPart],
    *,
    tool_manager: ToolExecutionManager,
    principal: str | None,
) -> list[Message]:
    """Dispatch tool calls; return tool-role messages to feed back to the LLM.

    AuthRequiredError propagates so the caller can react. All other
    :class:`MatrixError` instances are converted to
    ``ToolResultPart(error=True)`` by the manager itself; the
    defensive catch here is belt-and-braces for adapter bugs.
    """
    result_parts: list[ToolResultPart] = []
    for call in calls:
        try:
            rp = await tool_manager.execute(call, principal=principal)
        except AuthRequiredError:
            raise
        except MatrixError as exc:  # defence-in-depth.
            rp = ToolResultPart(id=call.id, output=str(exc), error=True)
        result_parts.append(rp)
    if not result_parts:
        return []
    return [Message(role="tool", parts=list(result_parts))]


__all__ = ["run_agent_turn"]
