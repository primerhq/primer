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
import functools
import json
from typing import TYPE_CHECKING, Any

from primer.agent.loop import run_agent_turn
from primer.agent.prompt_render import render_system_prompt_or_raw
from primer.agent.tool_manager import ToolExecutionManager
from primer.graph._node_identity import current_graph_node_id
from primer.graph._node_refs import (
    _NodeDone,
    _PendingAgentYield,
    _ToolDispatchBarrier,
    await_tool_dispatch_barrier,
)
from primer.graph.template import render_input_template
from primer.model.chat import Message, StreamEvent, TextPart
from primer.model.graph import GraphContext, NodeOutput, _AgentNodeRef
from primer.model.principal import PrincipalRef
from primer.model.yield_ import YieldToWorker


if TYPE_CHECKING:
    from collections.abc import Callable

    from primer.model.agent import Agent
    from primer.session.persistence import _CoalesceState


def _make_node_scoped_call_resolver(
    coalesce_state: "_CoalesceState", node_id: str,
) -> "Callable[[str], tuple[str, int]]":
    """Build the NODE-QUALIFIED resolver a graph node's tool-dispatch seam
    uses to turn a raw provider tool-call id into ``(scoped_id, record_seq)``.

    Graph-surface sibling of ``primer.session.dispatch._make_scoped_call_
    resolver``: SAME two-step lookup (``scoped_call_ids`` then
    ``tool_call_record_seq``), but keyed ``(node_id, raw_id)`` instead of
    ``(None, raw_id)`` -- concurrent fan-out siblings of the SAME base node
    reuse raw provider ids, so ``node_id`` (already the fan-out-instance-
    qualified id, e.g. ``"worker[0]"`` -- see ``current_graph_node_id``)
    disambiguates them exactly like every other per-node lookup on this
    surface (tool_names, scoped_call_ids itself). A PURE lookup, same as
    the chat/workspace version -- the graph surface's own ordering concern
    (the live fan-out queue race) is handled separately by
    ``await_dispatch_barrier``, never folded in here (01a0518b review
    ruling on fork (b)). Built FRESH at every dispatch call, never cached
    across turns or nodes -- ``coalesce_state`` is re-bound each turn and
    a node's own identity can change between dispatches (fan-out).
    """
    def _resolve(raw_call_id: str) -> tuple[str, int]:
        scoped_id = coalesce_state.scoped_call_ids.get((node_id, raw_call_id))
        if scoped_id is None:
            raise RuntimeError(
                f"resolve_scoped_call: no scoped id for node {node_id!r} "
                f"raw tool-call id {raw_call_id!r} - ToolCallStart never "
                "minted one this turn"
            )
        record_seq = coalesce_state.tool_call_record_seq.get(scoped_id)
        if record_seq is None:
            raise RuntimeError(
                f"resolve_scoped_call: scoped id {scoped_id!r} (node "
                f"{node_id!r}) has no durable TOOL_CALL record_seq yet - "
                "the durable-append-before-claimable invariant broke"
            )
        return scoped_id, record_seq
    return _resolve


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

    async def _resolve_node_llm(self, node: _AgentNodeRef, agent):
        """Resolve the adapter + model for one node, honouring its override.

        The second argument is passed ONLY when the node declares one. The
        resolver is caller-supplied and the overwhelming majority take a
        single ``agent``; calling those with two arguments would break every
        graph that never wanted an override in the first place.
        """
        if node.profile_id:
            return await self._llm_resolver(agent, node.profile_id)
        return await self._llm_resolver(agent)

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
        # These two fallbacks carry NO toolset providers, so the RBAC tool
        # floor is unreachable through them; still pass the system principal
        # (never None) to honour the invariant that every manager resolves
        # to a real invoker and to stay safe if tools are ever added here.
        if node.response_format is not None:
            return ToolExecutionManager(
                initiated_by=PrincipalRef.system(), turn_no=self._turn_no,
            )
        if self._tool_manager_resolver is not None:
            return await self._tool_manager_resolver(agent)
        return ToolExecutionManager(
            initiated_by=PrincipalRef.system(), turn_no=self._turn_no,
        )

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
        queue: "asyncio.Queue[StreamEvent | _NodeDone | _ToolDispatchBarrier]",
        *,
        extra_scope: dict[str, Any] | None = None,
    ) -> NodeOutput:
        """Run one agent-backed node; identical semantics to a standalone agent.

        ``extra_scope`` carries per-fan-out-instance vars (``fanout_index``,
        ``fanout_item``) for synthesized invocations (Spec B §2.1).
        """
        agent = await self._agent_resolver(node.agent_id)
        llm, llm_model = await self._resolve_node_llm(node, agent)
        tool_manager = await self._select_node_tool_manager(node, agent)

        # Render the input template -> single user-role Message.
        rendered = render_input_template(
            node.input_template, context=context, extra_scope=extra_scope
        )
        new_user_msg = Message(role="user", parts=[TextPart(text=rendered)])

        # 01a05935 item 3: instance-qualified, same reasoning as the
        # event-tagging line below - concurrent fan-out siblings of this
        # SAME agent node must not read/write the SAME history rows.
        # Both storage backends key purely on whatever string they're
        # given, so this is the complete fix (no storage-layer change).
        history_node_id = current_graph_node_id() or node.id

        # Build the prompt: system + history + new user msg.
        history = await self._load_node_history(history_node_id)
        prompt: list[Message] = []
        if agent.system_prompt:
            sys_text = render_system_prompt_or_raw(agent.system_prompt, context.ctx)
            prompt.append(
                Message(role="system", parts=[TextPart(text=sys_text)])
            )
        prompt.extend(history)
        prompt.append(new_user_msg)

        # 01a0518b (graph-surface boundary d): built FRESH for this
        # dispatch, never cached - history_node_id is THIS call's fan-out-
        # instance-qualified identity (same value the event-tagging line
        # below uses), and coalesce_state is re-bound every turn. None
        # when the executor never opted in (bind_coalesce_state was never
        # called), which keeps _dispatch_tool_calls's own gate a no-op -
        # byte-identical to today's in-process behaviour.
        resolve_scoped_call = None
        await_dispatch_barrier = None
        if self._coalesce_state is not None:
            resolve_scoped_call = _make_node_scoped_call_resolver(
                self._coalesce_state, history_node_id,
            )
            # Live fan-out: this node's own queue.put() calls above race
            # the drainer, unlike a pull-chain - see await_tool_dispatch_
            # barrier's own docstring.
            await_dispatch_barrier = functools.partial(
                await_tool_dispatch_barrier, queue,
            )

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
                artifact_storage=self._artifact_storage,
                turn_no=self._turn_no,
                tool_calls_as_claims_enabled=self._tool_calls_as_claims_enabled,
                resolve_scoped_call=resolve_scoped_call,
                await_dispatch_barrier=await_dispatch_barrier,
            ):
                # 01a0518f: current_graph_node_id() is the fan-out-
                # instance-qualified id (_stream_node sets it before
                # calling in), so concurrent siblings of this SAME agent
                # node tag their streamed events distinctly instead of
                # colliding on the shared base node.id - see
                # primer.graph._node_identity. Falls back to node.id when
                # unset (byte-identical to before that call path).
                await queue.put(
                    self._wrap_event(
                        event, current_graph_node_id() or node.id,
                        context.iteration,
                    )
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
        await self._persist_node_turn(history_node_id, context.iteration, all_new)

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
        llm, llm_model = await self._resolve_node_llm(node, agent)
        tool_manager = await self._select_node_tool_manager(node, agent)

        rendered = render_input_template(
            node.input_template, context=context, extra_scope=None
        )
        new_user_msg = Message(role="user", parts=[TextPart(text=rendered)])
        # 01a05935 item 3: pending.node_id is the fan-out-instance-
        # qualified id this specific park record belongs to (node.id
        # above is the base node's DEFINITION - _resolve_node_def
        # already collapsed the instance id to look it up), so it's the
        # authoritative key here, not a contextvar lookup - this method
        # isn't necessarily called from the same dispatch path that sets
        # current_graph_node_id, and pending.node_id is already exactly
        # right without depending on that ambient state being set.
        history = await self._load_node_history(pending.node_id)
        prompt: list[Message] = []
        if agent.system_prompt:
            sys_text = render_system_prompt_or_raw(agent.system_prompt, context.ctx)
            prompt.append(Message(role="system", parts=[TextPart(text=sys_text)]))
        prompt.extend(history)
        prompt.append(new_user_msg)
        rehydrated_assistant = [
            Message.model_validate(m) for m in pending.llm_messages
        ]
        prompt.extend(rehydrated_assistant)
        prompt.append(tool_result_msg)

        # 01a0518b (graph-surface boundary d): node-qualified via
        # pending.node_id (the (None, raw_id) key must never appear on
        # this surface, review ruling condition 3) even though this path
        # needs NO barrier - unlike _stream_agent_node this method is
        # called from the executor's own direct resume generator (graph/
        # base.py's resume_from_checkpoint), not the live fan-out queue:
        # no concurrent sibling shares a queue with it, so it has the
        # same pull-chain guarantee as the chat/workspace surface.
        resolve_scoped_call = None
        if self._coalesce_state is not None:
            resolve_scoped_call = _make_node_scoped_call_resolver(
                self._coalesce_state, pending.node_id,
            )

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
            artifact_storage=self._artifact_storage,
            turn_no=self._turn_no,
            tool_calls_as_claims_enabled=self._tool_calls_as_claims_enabled,
            resolve_scoped_call=resolve_scoped_call,
        ):
            pass

        all_new = [new_user_msg, *rehydrated_assistant, tool_result_msg, *produced_messages]
        await self._persist_node_turn(pending.node_id, pending.iteration, all_new)

        return self._agent_node_output(
            produced_messages, node.response_format,
            history + all_new, pending.iteration,
        )
