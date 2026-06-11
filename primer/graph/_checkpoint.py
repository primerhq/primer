"""Checkpoint serialisation mixin for the graph executor.

`_CheckpointMixin` owns the park/resume snapshot surface that
:class:`primer.graph.base._BaseGraphExecutor` mixes in:
``_build_pending_park_yield`` (build the outer approval yield for the
current pending set), ``snapshot_state`` (serialise mid-flight executor
state to a JSON-able dict), and ``restore_state`` (the inverse).

It is a mixin, not a standalone class: the methods read and write the
executor's mid-flight attributes (``_context``, ``_ready_set``,
``_node_states``, ``_fanout_instances``, ``_fanout_target_expected_count``,
``_instance_to_spec``, ``_fanout_drain_state``, ``_pending_toolcalls``,
``_pending_agent_yields``, ``_admitted``, ``_graph``), all of which are
initialised by ``_BaseGraphExecutor.__init__``. Keeping them here keeps
base.py focused on the superstep control flow.
"""

from __future__ import annotations

from typing import Any

from primer.graph._node_refs import (
    _FanoutDrainState,
    _FanoutInstance,
    _PendingAgentYield,
    _PendingToolCall,
)
from primer.model.graph import (
    FanOutSpec,
    GraphContext,
    NodeOutput,
    NodeRuntimeState,
)
from primer.model.yield_ import Yielded, YieldToWorker


class _CheckpointMixin:
    """Snapshot / restore / park-yield methods for `_BaseGraphExecutor`."""

    def _build_pending_park_yield(self) -> "YieldToWorker":
        """Build the outer approval ``YieldToWorker`` for the current
        pending human-interaction set (tool_call approvals + agent-node
        yields). Stamps ``event_keys`` (the full set) + the snapshot so
        the worker parks on all of them; used both when a superstep first
        yields and when re-parking on the remaining keys after one reply.
        """
        all_keys = (
            [p.parked_event_key for p in self._pending_toolcalls]
            + [p.event_key for p in self._pending_agent_yields]
        )
        if self._pending_toolcalls:
            first = self._pending_toolcalls[0]
            primary_event_key = first.parked_event_key
            primary_tcid = first.tool_call_id
            node_def = next(
                (n for n in self._graph.nodes if n.id == first.node_id), None
            )
            tool_id = getattr(node_def, "tool_id", None)
            resume_meta: dict[str, Any] = {}
            if tool_id is not None:
                resume_meta["original_call"] = {
                    "id": first.tool_call_id,
                    "name": tool_id,
                    "arguments": first.arguments,
                }
        else:
            first_ay = self._pending_agent_yields[0]
            primary_event_key = first_ay.event_key
            primary_tcid = first_ay.tool_call_id
            resume_meta = dict(first_ay.resume_metadata)
        yld = YieldToWorker(
            Yielded(
                tool_name="_approval",
                event_key=primary_event_key,
                resume_metadata=resume_meta,
                event_keys=all_keys,
            ),
            tool_call_id=primary_tcid,
        )
        yld.graph_checkpoint = self.snapshot_state()  # type: ignore[attr-defined]
        return yld

    # ---- Checkpoint payload (Phase 6 / Spec B §2.3 step 3) --------------

    def snapshot_state(self) -> dict[str, Any]:
        """Serialise the executor's mid-flight state into a JSON-compatible dict.

        Used by :meth:`invoke` when a ToolCall node yields for approval —
        the worker persists this payload onto the session's parked state,
        and a fresh executor calls :meth:`restore_state` on the resume
        path to reconstruct the world before draining the pending
        ToolCalls.

        Fields:

        * ``context`` — :class:`GraphContext` via ``model_dump(mode="json")``.
        * ``ready_set`` — the sorted list of node ids the outer loop was
          about to run when the yield fired (so resume can re-enter the
          superstep loop at the same point).
        * ``node_states`` — per-node :class:`NodeRuntimeState`, json-dumped.
        * ``fanout_instances`` — synthesized_id → instance dict.
        * ``fanout_target_expected_count`` — target_id → expected count.
        * ``instance_to_spec`` — synthesized_id →
          ``{"fanout_node_id": ..., "spec": <FanOutSpec.model_dump>}``.
        * ``fanout_drain_state`` — drain_key → drain_state dict.
        * ``pending_toolcalls`` — list of pending ToolCall dicts.
        """
        ctx_payload: dict[str, Any] | None = None
        if self._context is not None:
            ctx_payload = self._context.model_dump(mode="json")
        return {
            "context": ctx_payload,
            "ready_set": sorted(self._ready_set),
            "node_states": {
                nid: ns.model_dump(mode="json")
                for nid, ns in self._node_states.items()
            },
            "fanout_instances": {
                sid: {
                    "synthesized_id": inst.synthesized_id,
                    "target_node_id": inst.target_node_id,
                    "fanout_index": inst.fanout_index,
                    "fanout_item": (
                        inst.fanout_item.model_dump(mode="json")
                        if isinstance(inst.fanout_item, NodeOutput)
                        else inst.fanout_item
                    ),
                    "fanout_item_kind": (
                        "node_output"
                        if isinstance(inst.fanout_item, NodeOutput)
                        else "raw"
                    ),
                }
                for sid, inst in self._fanout_instances.items()
            },
            "fanout_target_expected_count": dict(
                self._fanout_target_expected_count
            ),
            "instance_to_spec": {
                sid: {
                    "fanout_node_id": fanout_id,
                    "spec": spec.model_dump(mode="json"),
                }
                for sid, (fanout_id, spec) in self._instance_to_spec.items()
            },
            "fanout_drain_state": {
                key: {
                    "on_failure": ds.on_failure,
                    "fanout_node_id": ds.fanout_node_id,
                    "target_node_id": ds.target_node_id,
                    "expected_count": ds.expected_count,
                    "completed_count": ds.completed_count,
                    "any_failed": ds.any_failed,
                    "first_failure": list(ds.first_failure) if ds.first_failure else None,
                }
                for key, ds in self._fanout_drain_state.items()
            },
            "pending_toolcalls": [
                {
                    "node_id": p.node_id,
                    "tool_call_id": p.tool_call_id,
                    "parked_event_key": p.parked_event_key,
                    "arguments": dict(p.arguments),
                }
                for p in self._pending_toolcalls
            ],
            "pending_agent_yields": [
                {
                    "node_id": p.node_id,
                    "tool_call_id": p.tool_call_id,
                    "event_key": p.event_key,
                    "tool_name": p.tool_name,
                    "resume_metadata": dict(p.resume_metadata),
                    "llm_messages": list(p.llm_messages),
                    "iteration": p.iteration,
                }
                for p in self._pending_agent_yields
            ],
            # Denormalised per-node dispatch info for tool-call nodes ONLY:
            # each bakes the graph node's tool_id into ``original_call``,
            # which the channel layer can't recompute without the graph.
            # Agent-yield dispatch entries are NOT stored here -- they are
            # derived from ``pending_agent_yields`` at send time (see
            # ``primer.worker.yield_runtime.merge_pending_dispatch``) so their
            # resume_metadata lives in the blob once, not twice.
            "pending_dispatch": [
                {
                    "kind": "_approval",
                    "tool_call_id": p.tool_call_id,
                    "resume_metadata": {
                        "original_call": {
                            "id": p.tool_call_id,
                            "name": getattr(
                                next((n for n in self._graph.nodes
                                      if n.id == p.node_id), None),
                                "tool_id", "<unknown>",
                            ),
                            "arguments": dict(p.arguments),
                        },
                    },
                }
                for p in self._pending_toolcalls
            ],
        }

    def restore_state(self, payload: dict[str, Any]) -> None:
        """Inverse of :meth:`snapshot_state` — repopulate executor attrs.

        The graph topology + resolvers stay as-passed at construction
        time; only the dynamic execution state is reconstructed. Callers
        that mutated the topology between checkpoint + resume are on
        their own (Spec B does not yet support graph hot-edits across
        a pause).
        """
        ctx_raw = payload.get("context")
        if ctx_raw is None:
            self._context = None
        else:
            self._context = GraphContext.model_validate(ctx_raw)
        self._ready_set = set(payload.get("ready_set") or [])
        # Re-seed the admitted-set used by the FanIn callable-router gate.
        # The restored ready-set is the in-flight frontier; completed nodes
        # already have output (so they never block). This keeps a resumed
        # executor from forgetting that a still-pending callable-router
        # source must gate a downstream FanIn.
        self._admitted = set(self._ready_set)
        if self._context is not None:
            self._admitted.update(self._context.nodes.keys())
        self._node_states = {
            nid: NodeRuntimeState.model_validate(raw)
            for nid, raw in (payload.get("node_states") or {}).items()
        }
        self._fanout_instances = {}
        for sid, raw in (payload.get("fanout_instances") or {}).items():
            kind = raw.get("fanout_item_kind", "raw")
            item_raw = raw.get("fanout_item")
            if kind == "node_output" and item_raw is not None:
                item: Any = NodeOutput.model_validate(item_raw)
            else:
                item = item_raw
            self._fanout_instances[sid] = _FanoutInstance(
                synthesized_id=raw["synthesized_id"],
                target_node_id=raw["target_node_id"],
                fanout_index=raw.get("fanout_index"),
                fanout_item=item,
            )
        self._fanout_target_expected_count = dict(
            payload.get("fanout_target_expected_count") or {}
        )
        self._instance_to_spec = {}
        for sid, raw in (payload.get("instance_to_spec") or {}).items():
            spec = FanOutSpec.model_validate(raw["spec"])
            self._instance_to_spec[sid] = (raw["fanout_node_id"], spec)
        self._fanout_drain_state = {}
        for key, raw in (payload.get("fanout_drain_state") or {}).items():
            ff = raw.get("first_failure")
            first_failure = tuple(ff) if ff else None
            self._fanout_drain_state[key] = _FanoutDrainState(
                on_failure=raw["on_failure"],
                fanout_node_id=raw["fanout_node_id"],
                target_node_id=raw["target_node_id"],
                expected_count=raw["expected_count"],
                completed_count=raw.get("completed_count", 0),
                any_failed=raw.get("any_failed", False),
                first_failure=first_failure,  # type: ignore[arg-type]
            )
        self._pending_toolcalls = [
            _PendingToolCall(
                node_id=raw["node_id"],
                tool_call_id=raw["tool_call_id"],
                parked_event_key=raw["parked_event_key"],
                arguments=dict(raw.get("arguments") or {}),
            )
            for raw in (payload.get("pending_toolcalls") or [])
        ]
        self._pending_agent_yields = [
            _PendingAgentYield(
                node_id=raw["node_id"],
                tool_call_id=raw["tool_call_id"],
                event_key=raw["event_key"],
                tool_name=raw["tool_name"],
                resume_metadata=dict(raw.get("resume_metadata") or {}),
                llm_messages=list(raw.get("llm_messages") or []),
                iteration=raw["iteration"],
            )
            for raw in (payload.get("pending_agent_yields") or [])
        ]
