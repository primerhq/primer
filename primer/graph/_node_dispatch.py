"""Per-node dispatch mixin for the graph executor.

`_NodeDispatchMixin` owns the per-node-kind dispatch that the superstep
loop drives: resolve a (possibly synthesized fan-out) node id to its
definition, run one node of any kind, and recurse into subgraph nodes.
It mixes into :class:`primer.graph.base._BaseGraphExecutor`:

* ``_resolve_node_def`` — map a node id (incl. ``"worker[2]"`` fan-out
  instances) to its node definition;
* ``_stream_node`` — run one node of any kind, pushing live events to a
  queue then a terminal ``_NodeDone`` (FanOut dispatch, FanIn / End / Begin
  data-shaping, ToolCall dispatch + approval-yield park, Agent / subgraph
  delegation, and the agent-node yield-park handling);
* ``_stream_subgraph_node`` — recurse into a child graph, forwarding its
  events under the parent node id and capturing its End / error outcome.

It is a mixin, not a standalone class: the methods read the executor's
construction-time indices and mid-flight bookkeeping (``_fanout_instances``,
``_nodes_by_id``, ``_pending_fanout``, ``_fanout_target_expected_count``,
``_instance_to_spec``, ``_fanout_drain_state``, ``_pending_toolcalls``,
``_pending_agent_yields``, ``_graph_resolver``) and call sibling methods
that remain on / are mixed into ``_BaseGraphExecutor``
(``_dispatch_toolcall``, ``_build_sub_executor``, ``_stream_agent_node``,
``_wrap_event``). All are provided by the concrete executor via the MRO.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from primer.graph._node_refs import (
    _FanoutDrainState,
    _FanoutInstance,
    _FanoutSourceInvalid,
    _GraphEndOutputEvent,
    _GraphErrorEvent,
    _NodeDone,
    _PendingAgentYield,
    _PendingToolCall,
    _map_toolcall_result,
    _materialise_begin_output,
    _render_end_output,
    _render_fanin_output,
    _resolve_fanout_spec,
    _resolve_toolcall_arguments,
)
from primer.graph.template import render_input_template
from primer.model.chat import Message, StreamEvent, TextPart
from primer.model.except_ import ConfigError
from primer.model.graph import (
    FanOutSpec,
    GraphContext,
    NodeOutput,
    _AgentNodeRef,
    _BeginNode,
    _EndNode,
    _FanInNode,
    _FanOutNode,
    _GraphNodeRef,
    _ToolCallNode,
)
from primer.model.yield_ import YieldToWorker


class _SubgraphFailed(Exception):
    """A child graph (a ``graph`` node's delegate) ended ``failed``.

    Raised by :meth:`_BaseGraphExecutor._stream_subgraph_node` so the parent's
    graph node is recorded as a FAILED node (via the standard
    ``_NodeDone(error=...)`` path) instead of silently succeeding with empty
    output. Honors a fan-out spec's ``on_failure`` policy like any other node
    failure.
    """


class _NodeDispatchMixin:
    """Per-node-kind dispatch + subgraph recursion for `_BaseGraphExecutor`."""

    def _resolve_node_def(self, node_id: str):
        """Resolve ``node_id`` to its node definition.

        Synthesized fan-out instance ids (e.g. ``"worker[2]"``) resolve to
        their target node's definition; all other ids resolve directly via
        ``_nodes_by_id``. Spec B §2.1.
        """
        instance = self._fanout_instances.get(node_id)
        if instance is not None:
            return self._nodes_by_id[instance.target_node_id]
        return self._nodes_by_id[node_id]

    async def _stream_node(
        self,
        node_id: str,
        context: GraphContext,
        queue: "asyncio.Queue[StreamEvent | _NodeDone]",
    ) -> None:
        """Run one node; push events live to ``queue``, then a _NodeDone.

        Spec B §2.1: synthesized fan-out instance ids (``worker[2]`` etc.)
        resolve to the underlying target node definition, and the executor
        renders the node's input_template against a Jinja scope that includes
        ``fanout_index`` and ``fanout_item``.
        """
        instance = self._fanout_instances.get(node_id)
        if instance is not None:
            node = self._nodes_by_id[instance.target_node_id]
            extra_scope: dict[str, Any] | None = {
                "fanout_index": instance.fanout_index,
                "fanout_item": instance.fanout_item,
            }
        else:
            node = self._nodes_by_id[node_id]
            extra_scope = None
        try:
            if isinstance(node, _FanOutNode):
                # FanOut is a pure dispatcher (Spec B §2.1):
                # 1) Build its own bookkeeping NodeOutput.
                # 2) Resolve every spec into instances.
                # 3) Stash the instance plan on the executor so the outer
                #    superstep loop drains them into next_ready.
                fanout_self_output = NodeOutput(
                    text=json.dumps(
                        {"node_id": node.id, "specs": len(node.specs)}
                    ),
                    parsed=None,
                    history=[],
                    iteration=context.iteration,
                )
                try:
                    all_instances: list[_FanoutInstance] = []
                    # Track which spec each instance came from so the per-node
                    # result handler can look up ``on_failure`` (Spec B §2.5).
                    instance_specs: list[tuple[_FanoutInstance, FanOutSpec]] = []
                    for spec in node.specs:
                        spec_insts = _resolve_fanout_spec(
                            spec, context, fanout_self_output
                        )
                        all_instances.extend(spec_insts)
                        for inst in spec_insts:
                            instance_specs.append((inst, spec))
                except _FanoutSourceInvalid as exc:
                    await queue.put(
                        _NodeDone(
                            node_id=node.id,
                            output=None,
                            error=exc.reason,
                            ended_detail="fanout_source_invalid",
                        )
                    )
                    return
                # Record the FanOut's own NodeOutput so downstream conditional
                # edges from FanOut can read it.
                await queue.put(
                    _NodeDone(
                        node_id=node.id,
                        output=fanout_self_output,
                        error=None,
                    )
                )
                # Stash plan + per-target expected counts for FanIn ready-set.
                self._pending_fanout[node.id] = all_instances
                counts: dict[str, int] = {}
                for inst in all_instances:
                    counts[inst.target_node_id] = (
                        counts.get(inst.target_node_id, 0) + 1
                    )
                for tgt, n in counts.items():
                    self._fanout_target_expected_count[tgt] = max(
                        self._fanout_target_expected_count.get(tgt, 0), n
                    )
                # Spec B §2.5 — populate per-instance spec lookup + per-(FanOut,
                # target) drain state so the outer loop's result-application
                # path can branch on ``on_failure``.
                #
                # When a spec is "fail_fast" we still register it so the
                # per-node handler's lookup is consistent; the handler only
                # consults the drain state for non-fail_fast modes, so the
                # bookkeeping cost is one tuple per instance.
                for inst, spec in instance_specs:
                    self._instance_to_spec[inst.synthesized_id] = (
                        node.id, spec,
                    )
                # Build / refresh one drain-state entry per (fanout, target).
                # Key uses '__' separator since target ids are normal identifiers.
                spec_target_counts: dict[tuple[str, str], int] = {}
                spec_by_target: dict[tuple[str, str], FanOutSpec] = {}
                for inst, spec in instance_specs:
                    key = (node.id, inst.target_node_id)
                    spec_target_counts[key] = (
                        spec_target_counts.get(key, 0) + 1
                    )
                    # All instances for one (fanout, target) belong to the
                    # same FanOutSpec by construction (each spec emits to
                    # exactly one target id for broadcast/map; tee writes
                    # one instance per target).
                    spec_by_target[key] = spec
                for (fanout_id, target_id), expected in spec_target_counts.items():
                    drain_key = f"{fanout_id}__{target_id}"
                    spec = spec_by_target[(fanout_id, target_id)]
                    self._fanout_drain_state[drain_key] = _FanoutDrainState(
                        on_failure=spec.on_failure,
                        fanout_node_id=fanout_id,
                        target_node_id=target_id,
                        expected_count=expected,
                    )
                return
            if isinstance(node, _FanInNode):
                # FanIn is a pure data-shaping aggregator (Spec B §2.2):
                # render aggregate_template + optional output_schema, then
                # post a _NodeDone with ended_detail set on failure so the
                # outer loop terminates `failed`.
                fres = _render_fanin_output(node, context)
                if fres.error_code is not None:
                    await queue.put(
                        _NodeDone(
                            node_id=node_id,
                            output=None,
                            error=fres.error_message or fres.error_code,
                            ended_detail=fres.error_code,
                        )
                    )
                    return
                fan_out = NodeOutput(
                    text=fres.text,
                    parsed=fres.parsed,
                    history=[],
                    iteration=context.iteration,
                )
                await queue.put(
                    _NodeDone(node_id=node_id, output=fan_out, error=None)
                )
                return
            if isinstance(node, _EndNode):
                # End is pure data-shaping; render output_template + optional
                # schema validation, then post a _NodeDone with ended_detail
                # set on failure so the outer loop terminates `failed`.
                res = _render_end_output(node, context)
                if res.error_code is not None:
                    await queue.put(
                        _NodeDone(
                            node_id=node_id,
                            output=None,
                            error=res.error_message or res.error_code,
                            ended_detail=res.error_code,
                        )
                    )
                    return
                out = NodeOutput(
                    text=res.text,
                    parsed=res.parsed,
                    history=[],
                    iteration=context.iteration,
                )
                # Spec §4.4 — emit an End-output event so the session
                # translator can append an ``assistant_token`` record
                # to messages.jsonl carrying the graph's final output.
                # Storage-backed taps that don't care just drop it.
                await queue.put(
                    _GraphEndOutputEvent(  # type: ignore[arg-type]
                        text=res.text,
                        parsed=res.parsed,
                        end_node_id=node_id,
                    )
                )
                await queue.put(
                    _NodeDone(node_id=node_id, output=out, error=None)
                )
                return
            if isinstance(node, _ToolCallNode):
                # Spec B §2.3 — ToolCall fires the configured tool via the
                # executor's _dispatch_toolcall hook (workspace_executor
                # wires the workspace session's ToolExecutionManager; tests
                # inject a stub). Phase 3 covers the happy path + failure
                # mapping; Phase 6 wires the approval-yielding path
                # (`_GraphToolCallYield`).
                try:
                    args = _resolve_toolcall_arguments(
                        node, context, extra_scope=extra_scope
                    )
                except Exception as exc:  # noqa: BLE001 -- Jinja / JSON parse
                    await queue.put(
                        _NodeDone(
                            node_id=node_id,
                            output=None,
                            error=str(exc),
                            ended_detail="template_error",
                        )
                    )
                    return
                try:
                    result = await self._dispatch_toolcall(node, args)
                except YieldToWorker as yld:
                    # Spec B §2.3 step 3 / Phase 6 — the tool engine raised
                    # YieldToWorker because the approval gate fired. Defer
                    # the ToolCall: record a pending entry and post a
                    # suspended sentinel so the outer loop knows to leave
                    # this node's status unchanged. The executor saves a
                    # checkpoint after the superstep settles and re-raises
                    # YieldToWorker upward; the worker catches it, parks
                    # the session, and resumes via
                    # :meth:`_BaseGraphExecutor.resume_from_checkpoint`
                    # once the operator approves.
                    self._pending_toolcalls.append(
                        _PendingToolCall(
                            node_id=node_id,
                            tool_call_id=yld.tool_call_id,
                            parked_event_key=yld.yielded.event_key,
                            arguments=args,
                            tool_name=yld.yielded.tool_name,
                            resume_metadata=dict(
                                yld.yielded.resume_metadata or {}
                            ),
                        )
                    )
                    await queue.put(
                        _NodeDone(
                            node_id=node_id,
                            output=None,
                            error=None,
                            ended_detail=None,
                            suspended=True,
                        )
                    )
                    return
                except Exception as exc:  # noqa: BLE001
                    await queue.put(
                        _NodeDone(
                            node_id=node_id,
                            output=None,
                            error=str(exc),
                            ended_detail="tool_execution_failed",
                        )
                    )
                    return
                mapped = _map_toolcall_result(
                    result, output_schema=node.output_schema
                )
                if mapped.error_code is not None:
                    await queue.put(
                        _NodeDone(
                            node_id=node_id,
                            output=None,
                            error=mapped.error_message or mapped.error_code,
                            ended_detail=mapped.error_code,
                        )
                    )
                    return
                tc_out = NodeOutput(
                    text=mapped.text,
                    parsed=mapped.parsed,
                    history=[],
                    iteration=context.iteration,
                )
                await queue.put(
                    _NodeDone(node_id=node_id, output=tc_out, error=None)
                )
                return
            if isinstance(node, _BeginNode):
                # Begin is pure data-shaping; no LLM call, no events emitted.
                # The base executor stores initial_input as a list[Message];
                # the workspace executor (Phase 4) widens that union to
                # also carry dict/str via session metadata.
                gi = context.initial_input
                if isinstance(gi, list):
                    output: NodeOutput | None = _materialise_begin_output(
                        graph_input=None, initial_messages=gi
                    )
                else:
                    output = _materialise_begin_output(
                        graph_input=gi, initial_messages=[]
                    )
            elif isinstance(node, _GraphNodeRef):
                output = await self._stream_subgraph_node(
                    node, context, queue, extra_scope=extra_scope
                )
            elif isinstance(node, _AgentNodeRef):
                output = await self._stream_agent_node(
                    node, context, queue, extra_scope=extra_scope
                )
            else:  # pragma: no cover -- discriminated union exhausted
                raise ConfigError(
                    f"unknown node kind: {type(node).__name__}"
                )
            await queue.put(
                _NodeDone(node_id=node_id, output=output, error=None)
            )
        except YieldToWorker as yld:
            if isinstance(node, _AgentNodeRef):
                # Defer the agent node: record a pending agent-yield and
                # post a suspended sentinel so the superstep leaves it
                # unresolved. The executor checkpoints + re-raises after
                # the superstep settles (mirrors the ToolCall path).
                # Unified nested-yield: when the node's agent turn yielded from
                # INSIDE a nested invoke_agent invocation, ``yld.frames`` carries
                # the in-flight subagent chain (root-first) and ``yld.yielded``
                # is the deeper leaf. Preserve both so the worker can run the
                # continuation walk on resume; an empty stack (the node's own
                # ask_user / approval gate) keeps this park byte-identical.
                from primer.worker.frames import frames_to_jsonable
                nested_frames = list(getattr(yld, "frames", None) or [])
                self._pending_agent_yields.append(
                    _PendingAgentYield(
                        node_id=node_id,
                        tool_call_id=yld.tool_call_id,
                        event_key=yld.yielded.event_key,
                        tool_name=yld.yielded.tool_name,
                        resume_metadata=dict(yld.yielded.resume_metadata or {}),
                        llm_messages=list(yld.llm_messages or []),
                        iteration=context.iteration,
                        frames=frames_to_jsonable(nested_frames) if nested_frames else [],
                        leaf=yld.yielded.to_jsonable() if nested_frames else None,
                    )
                )
                await queue.put(
                    _NodeDone(
                        node_id=node_id, output=None, error=None,
                        ended_detail=None, suspended=True,
                    )
                )
                return
            # Non-agent yield (e.g. from a subgraph node): preserve the
            # prior behaviour of recording it as a node error.
            await queue.put(
                _NodeDone(node_id=node_id, output=None, error=yld)
            )
        except BaseException as exc:
            await queue.put(
                _NodeDone(node_id=node_id, output=None, error=exc)
            )
            if isinstance(exc, asyncio.CancelledError):
                raise

    async def _stream_subgraph_node(
        self,
        node: _GraphNodeRef,
        context: GraphContext,
        queue: "asyncio.Queue[StreamEvent | _NodeDone]",
        *,
        extra_scope: dict[str, Any] | None = None,
    ) -> NodeOutput:
        """Recurse into a subgraph; forward events under the parent node id.

        ``extra_scope`` carries per-fan-out-instance vars (``fanout_index``,
        ``fanout_item``) for synthesized invocations (Spec B §2.1).
        """
        if self._graph_resolver is None:
            raise ConfigError(
                f"subgraph node {node.id!r} requires a graph_resolver "
                "to be passed to the executor's constructor"
            )
        sub_graph = await self._graph_resolver(node.graph_id)
        # Fan-out instances of the SAME subgraph node share node.id; give each
        # its own child state subtree (``<node>[i]``) so concurrent broadcast/
        # map instances don't collide on one state.json / nodes/ tree.
        instance_suffix = ""
        if extra_scope is not None:
            fanout_index = extra_scope.get("fanout_index")
            if fanout_index is not None:
                instance_suffix = f"[{fanout_index}]"
        sub_executor = await self._build_sub_executor(
            node, sub_graph, instance_suffix=instance_suffix
        )

        rendered = render_input_template(
            node.input_template, context=context, extra_scope=extra_scope
        )
        sub_input = [Message(role="user", parts=[TextPart(text=rendered)])]

        # Forward every sub-event under THIS node's id so external taps
        # see the parent-graph node's namespace, not the inner one.
        # Track text deltas to assemble a text NodeOutput for downstream
        # consumers. The runtime terminal-event dataclasses
        # (_GraphErrorEvent, _GraphEndOutputEvent) aren't real
        # :class:`StreamEvent`s and don't survive ``_wrap_event``'s
        # ``.type`` access — forward them as-is so the parent's
        # aggregator can pass them on to taps.
        # Two terminal events carry the child's actual result: capture them
        # (don't merely forward to taps). The streamed text-delta accumulation
        # is only a FALLBACK for stubs / node-level text with no end-output
        # event -- it must NOT shadow the canonical End output. Mirrors
        # primer.graph.invoke_graph's two-channel handling.
        text_buf: list[str] = []
        end_output: _GraphEndOutputEvent | None = None
        sub_error: _GraphErrorEvent | None = None
        async for sub_event in sub_executor.invoke(sub_input):
            if isinstance(sub_event, _GraphEndOutputEvent):
                end_output = sub_event
                await queue.put(sub_event)  # type: ignore[arg-type]
                continue
            if isinstance(sub_event, _GraphErrorEvent):
                sub_error = sub_event
                await queue.put(sub_event)  # type: ignore[arg-type]
                continue
            await queue.put(
                self._wrap_event(sub_event, node.id, context.iteration)
            )
            ev_type = getattr(sub_event, "type", None)
            if ev_type == "text-delta":
                delta = getattr(sub_event, "text", None)
                if delta:
                    text_buf.append(delta)

        # A failed child must fail the parent node, not be silently swallowed
        # (otherwise the parent advances past a broken subgraph). A child can
        # fail two ways: a terminal _GraphErrorEvent (tool / routing / approval
        # failures) or a node-execution failure that ends the run "failed" with
        # NO terminal event -- so check the child's recorded outcome as well.
        # The raise surfaces as a FAILED node via _NodeDone(error=...) and
        # honors a fan-out spec's on_failure policy.
        child_ended = getattr(sub_executor, "_last_ended_reason", None)
        if sub_error is not None or child_ended == "failed":
            detail = (
                sub_error.message
                if sub_error is not None
                else getattr(sub_executor, "_last_ended_detail", None)
                or "child graph ended failed"
            )
            raise _SubgraphFailed(
                f"subgraph {node.graph_id!r} (node {node.id!r}) failed: {detail}"
            )

        # The child's End-node output is the canonical subgraph result; expose
        # its text + parsed to the parent. Fall back to streamed text only when
        # no end-output event was observed (e.g. a stub / node-level stream).
        if end_output is not None:
            return NodeOutput(
                text=end_output.text,
                parsed=end_output.parsed,
                history=[],
                iteration=context.iteration,
            )
        return NodeOutput(
            text="".join(text_buf),
            parsed=None,
            history=[],
            iteration=context.iteration,
        )
