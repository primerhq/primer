"""Agent-node turn machinery mixin for the graph executor.

`_AgentNodeMixin` owns how an agent-backed graph node runs a turn and how
a parked agent node resumes:

* ``_select_node_tool_manager`` — pick (or suppress) tools for the node;
* ``_agent_node_output`` — shape a NodeOutput from the produced messages;
* ``_stream_agent_node`` — run one agent turn, streaming events, capturing
  a yielding-tool park onto the YieldToWorker for the resume path;
* ``_resume_agent_node`` — rebuild and continue a parked node's turn with
  the human's tool result injected.

It is a mixin: the methods read the executor's resolvers / principal /
context (``_agent_resolver``, ``_llm_resolver``, ``_tool_manager_resolver``,
``_principal``, ``_context``) and call sibling methods that remain on
``_BaseGraphExecutor`` (``_load_node_history``, ``_persist_node_turn``,
``_resolve_node_def``, ``_wrap_event``). All are provided by the concrete
executor via the MRO.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from primer.agent.loop import run_agent_turn
from primer.agent.prompt_render import render_system_prompt
from primer.agent.tool_manager import ToolExecutionManager
from primer.graph._node_refs import _NodeDone, _PendingAgentYield
from primer.graph.template import render_input_template
from primer.model.chat import Message, StreamEvent, TextPart
from primer.model.graph import GraphContext, NodeOutput, _AgentNodeRef
from primer.model.yield_ import YieldToWorker


if TYPE_CHECKING:
    from primer.model.agent import Agent


def _strip_json_fences(text: str) -> str:
    """Strip a single wrapping markdown code fence from ``text``.

    Local models habitually wrap structured output in `````json ... ``````
    fences even when a ``response_format`` JSON schema is requested -- backends
    like LM Studio / llama.cpp treat the schema as a soft hint, not constrained
    decoding, so the fence survives. A plain ``json.loads`` then fails and the
    node's ``parsed`` is silently lost, which breaks any ``json_path`` router
    gating on a parsed field (the gate sees nothing and falls through to its
    default branch -- e.g. a loop that never converges). Tolerate the common
    fence shapes so the gate still sees the structured verdict. A string with
    no leading fence is returned unchanged (only surrounding whitespace
    trimmed), so raw-JSON output is unaffected.
    """
    s = text.strip()
    if s.startswith("```"):
        newline = s.find("\n")
        if newline != -1:
            s = s[newline + 1:]  # drop the opening ``` / ```json line
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]  # drop the closing fence
    return s.strip()


class _AgentNodeMixin:
    """Agent-node turn + resume methods for `_BaseGraphExecutor`."""

    async def _select_node_tool_manager(
        self, node: _AgentNodeRef, agent: "Agent",
    ) -> ToolExecutionManager:
        """Pick the tool manager for an agent node. A structured-output
        node (``response_format`` set) is offered NO tools: the workspace
        holder auto-injects tools into every node, and grammar-based
        providers (LM Studio / llama.cpp / Ollama) reject a forced
        json_schema combined with tools ("cannot combine structured
        output constraints with lazy grammar"). Otherwise use the
        resolver, else an empty manager.
        """
        if node.response_format is not None:
            return ToolExecutionManager()
        if self._tool_manager_resolver is not None:
            return await self._tool_manager_resolver(agent)
        return ToolExecutionManager()

    def _agent_node_output(
        self,
        produced_messages: list[Message],
        response_format: dict[str, Any] | None,
        history: list[Message],
        iteration: int,
    ) -> NodeOutput:
        """Build a NodeOutput from an agent turn's produced messages: the
        last assistant message's text, plus ``parsed`` (JSON) when the node
        had a ``response_format``."""
        last_assistant: Message | None = None
        for msg in reversed(produced_messages):
            if msg.role == "assistant":
                last_assistant = msg
                break
        text = ""
        if last_assistant is not None:
            text = "".join(
                p.text  # type: ignore[union-attr]
                for p in last_assistant.parts
                if p.type == "text"
            )
        parsed: dict[str, Any] | None = None
        if response_format is not None and text:
            try:
                loaded = json.loads(_strip_json_fences(text))
                parsed = loaded if isinstance(loaded, dict) else {"value": loaded}
            except json.JSONDecodeError:
                parsed = None
        return NodeOutput(
            text=text, parsed=parsed, history=history, iteration=iteration,
        )

    async def _stream_agent_node(
        self,
        node: _AgentNodeRef,
        context: GraphContext,
        queue: "asyncio.Queue[StreamEvent | _NodeDone]",
        *,
        extra_scope: dict[str, Any] | None = None,
    ) -> NodeOutput:
        """Run one agent-backed node; identical semantics to a standalone agent.

        ``extra_scope`` carries per-fan-out-instance vars (``fanout_index``,
        ``fanout_item``) for synthesized invocations (Spec B §2.1).
        """
        agent = await self._agent_resolver(node.agent_id)
        llm, llm_model = await self._llm_resolver(agent)
        tool_manager = await self._select_node_tool_manager(node, agent)

        # Render the input template -> single user-role Message.
        rendered = render_input_template(
            node.input_template, context=context, extra_scope=extra_scope
        )
        new_user_msg = Message(role="user", parts=[TextPart(text=rendered)])

        # Build the prompt: system + history + new user msg.
        history = await self._load_node_history(node.id)
        prompt: list[Message] = []
        if agent.system_prompt:
            sys_text = render_system_prompt(agent.system_prompt, context.ctx)
            prompt.append(
                Message(role="system", parts=[TextPart(text=sys_text)])
            )
        prompt.extend(history)
        prompt.append(new_user_msg)

        # Delegate to the shared agent loop. Tool dispatch (multi-turn
        # if the LLM emits ToolCallParts) happens transparently here --
        # graph nodes get the same behaviour as standalone agents.
        produced_messages: list[Message] = []
        try:
            async for event in run_agent_turn(
                agent=agent,
                llm=llm,
                llm_model=llm_model,
                tool_manager=tool_manager,
                prompt=prompt,
                response_format=node.response_format,
                principal=self._principal,
                messages_out=produced_messages,
            ):
                await queue.put(
                    self._wrap_event(event, node.id, context.iteration)
                )
        except YieldToWorker as yld:
            # A yielding tool (ask_user) or an approval gate fired. The
            # standalone agent executor stamps the in-progress assistant
            # turn onto the exception; graph nodes call run_agent_turn
            # directly, so do it here so the resume path can rehydrate it.
            if not yld.llm_messages:
                yld.llm_messages = [
                    m.model_dump(mode="json") for m in produced_messages
                ]
            raise

        # Persist the new user msg + every message produced this turn
        # (assistant + any tool result messages from the loop).
        all_new = [new_user_msg] + produced_messages
        await self._persist_node_turn(node.id, context.iteration, all_new)

        return self._agent_node_output(
            produced_messages, node.response_format,
            history + all_new, context.iteration,
        )

    async def _resume_agent_node(
        self,
        pending: "_PendingAgentYield",
        tool_result_msg: Message,
    ) -> NodeOutput:
        """Continue a parked agent node's turn with the injected tool result.

        Rebuilds the prompt from: system + persisted node history +
        re-rendered input_template (deterministic against the restored
        context) + the rehydrated in-progress assistant turn + the
        ``tool_result_msg`` (the human's ask_user answer / approval
        verdict), then continues ``run_agent_turn`` to completion and
        returns the node's NodeOutput.
        """
        node = self._resolve_node_def(pending.node_id)
        assert isinstance(node, _AgentNodeRef)
        context = self._context
        assert context is not None
        agent = await self._agent_resolver(node.agent_id)
        llm, llm_model = await self._llm_resolver(agent)
        tool_manager = await self._select_node_tool_manager(node, agent)

        rendered = render_input_template(
            node.input_template, context=context, extra_scope=None
        )
        new_user_msg = Message(role="user", parts=[TextPart(text=rendered)])
        history = await self._load_node_history(node.id)
        prompt: list[Message] = []
        if agent.system_prompt:
            sys_text = "\n\n".join(agent.system_prompt)
            prompt.append(Message(role="system", parts=[TextPart(text=sys_text)]))
        prompt.extend(history)
        prompt.append(new_user_msg)
        rehydrated_assistant = [
            Message.model_validate(m) for m in pending.llm_messages
        ]
        prompt.extend(rehydrated_assistant)
        prompt.append(tool_result_msg)

        produced_messages: list[Message] = []
        async for _event in run_agent_turn(
            agent=agent,
            llm=llm,
            llm_model=llm_model,
            tool_manager=tool_manager,
            prompt=prompt,
            response_format=node.response_format,
            principal=self._principal,
            messages_out=produced_messages,
        ):
            pass

        all_new = [new_user_msg, *rehydrated_assistant, tool_result_msg, *produced_messages]
        await self._persist_node_turn(node.id, pending.iteration, all_new)

        return self._agent_node_output(
            produced_messages, node.response_format,
            history + all_new, pending.iteration,
        )
