"""Ready-set / edge-routing mixin for the graph executor.

`_RoutingMixin` owns how the Pregel-style superstep loop advances the
frontier: given the nodes that just ran, walk their outgoing edges,
resolve conditional (json_path / callable) routers, and gate FanIn
targets on upstream completion. It mixes into
:class:`primer.graph.base._BaseGraphExecutor`:

* ``_compute_next_ready`` - walk outgoing edges from the just-ran set,
  resolve every edge to a concrete target, and admit the next ready set
  (deferring FanIn targets until their upstreams are complete);
* ``_fanin_ready`` - decide whether a FanIn node may fire yet;
* ``_evaluate_conditional`` - resolve a single conditional edge to its
  target node id (json_path branch match / callable router resolve).

It is a mixin, not a standalone class: the methods read the executor's
construction-time topology indices and mid-flight bookkeeping
(``_fanout_instances``, ``_edges_by_from``, ``_edges_by_to``,
``_nodes_by_id``, ``_admitted``, ``_fanout_target_expected_count``,
``_callable_router_sources``, ``_router_registry``), all initialised by
``_BaseGraphExecutor.__init__``. Keeping them here keeps base.py focused
on the superstep control flow.
"""

from __future__ import annotations

from primer.graph._node_refs import _RoutingFailed
from primer.graph.router import first_matching_branch
from primer.model.except_ import ConfigError
from primer.model.graph import (
    GraphContext,
    _CallableRouter,
    _ConditionalEdge,
    _FanInNode,
    _JsonPathRouter,
    _StaticEdge,
)


class _RoutingMixin:
    """Ready-set computation + edge routing for `_BaseGraphExecutor`."""

    async def _compute_next_ready(
        self,
        just_ran: set[str],
        context: GraphContext,
    ) -> set[str]:
        """Walk outgoing edges from ``just_ran``; return the next ready set.

        Spec B §2.2: for FanIn targets, defer admission until every incoming
        edge's source has produced output (treating fan-out targets as the
        full set of synthesized instances).

        Synthesized fan-out instance ids (e.g. ``"worker[2]"``) don't carry
        outgoing edges of their own — the executor walks the edges of the
        underlying target node id (e.g. ``"worker"``) instead. This keeps
        graph authors free to write the natural ``worker -> fanin`` edge
        once even when ``worker`` is fan-out target with N instances.
        """
        # Build the effective edge-source set: each just-ran id contributes
        # its own outgoing edges; synthesized fan-out instances also
        # contribute their bare target's outgoing edges (de-duplicated).
        edge_sources: set[str] = set(just_ran)
        for nid in just_ran:
            inst = self._fanout_instances.get(nid)
            if inst is not None:
                edge_sources.add(inst.target_node_id)
        # Phase 1: resolve every outgoing edge to its concrete target. We must
        # know the FULL set of nodes scheduled this pass BEFORE gating any
        # FanIn, because a callable router resolved here may schedule a node
        # that itself feeds the FanIn (and the FanIn must then wait for it).
        candidates: list[str] = []
        for nid in edge_sources:
            for edge in self._edges_by_from.get(nid, []):
                if isinstance(edge, _StaticEdge):
                    candidates.append(edge.to_node)
                else:  # _ConditionalEdge
                    target_opt = await self._evaluate_conditional(edge, context)
                    if target_opt is None:
                        continue
                    candidates.append(target_opt)
        # Every resolved target is now "live" (admitted at least once); a
        # callable-router source admitted here is one the FanIn gate must
        # still wait on if it has not yet produced output.
        self._admitted.update(candidates)
        # Phase 2: admit, gating FanIn targets on upstream completion.
        next_ready: set[str] = set()
        for target in candidates:
            target_node = self._nodes_by_id.get(target)
            if isinstance(target_node, _FanInNode):
                if not self._fanin_ready(target_node, context):
                    continue
            next_ready.add(target)
        self._admitted.update(next_ready)
        return next_ready

    def _fanin_ready(
        self, node: "_FanInNode", context: GraphContext
    ) -> bool:
        """Return True iff every incoming edge's source has produced output.

        Spec B §2.2. Three upstream kinds are gated:

        * static / json-path-conditional edges: the source must have a
          ``NodeOutput`` in ``context.nodes`` (``_edges_by_to`` index).
        * fan-out sources: all N synthesized instances must have produced
          output (compare non-``None`` count against the spawning FanOut's
          expected instance count; the aggregator list is positionally
          aligned and may carry ``None`` placeholders).
        * callable-router sources: the router's target is unknown
          statically, so any callable-router source that is *live* (has been
          admitted this run but has not yet produced output) is treated as a
          potential upstream the FanIn must wait for. Once such a source
          produces output its routing decision is settled: if it routed here
          the existing static/json-path or list checks above already account
          for it; if it routed elsewhere it simply never blocks. A
          callable-router source that never activates is never ``_admitted``,
          so it cannot dead-lock the FanIn.
        """
        for edge in self._edges_by_to.get(node.id, []):
            src = getattr(edge, "from_node", None)
            if src is None:
                continue
            entry = context.nodes.get(src)
            if entry is None:
                return False
            if isinstance(entry, list):
                expected = self._fanout_target_expected_count.get(src)
                # The aggregator list is positionally aligned and may carry
                # ``None`` placeholders for instances that have not reported
                # yet. Count only the slots that have actually produced output
                # (``len`` would overcount past-end padding and undercount is
                # impossible since we pad-to-index).
                produced = sum(1 for x in entry if x is not None)
                if expected is None or produced < expected:
                    return False
        # Callable-router upstreams: a source that has been admitted this run
        # but has not yet produced output may still route into this FanIn, so
        # defer admission until it resolves.
        for src in self._callable_router_sources:
            if src in self._admitted and context.nodes.get(src) is None:
                return False
        return True

    async def _evaluate_conditional(
        self,
        edge: _ConditionalEdge,
        context: GraphContext,
    ) -> str | None:
        source_output = context.nodes.get(edge.from_node)
        if source_output is None:
            raise ConfigError(
                f"conditional edge from {edge.from_node!r} fired before "
                "the source node produced output"
            )
        router = edge.router
        if isinstance(router, _JsonPathRouter):
            # ``parsed is None`` means the source node's structured output
            # did not parse as JSON -- e.g. a model wrapped it in a ```json
            # fence, or the node carries no ``response_format``. Treat it as
            # "no branch matched" rather than crashing the whole graph: route
            # to ``default_to`` when set, otherwise raise the CODED
            # ``_RoutingFailed`` (``ended_detail='routing_failed'``) instead
            # of an uncoded ``ConfigError`` the executor swallows into a
            # detail-less failure.
            parsed = source_output.parsed
            match = (
                first_matching_branch(parsed, router.branches)
                if parsed is not None
                else None
            )
            if match is not None:
                target = match.to_node
            elif router.default_to is not None:
                target = router.default_to
            else:
                why = (
                    " (source produced no parsed JSON output)"
                    if parsed is None
                    else ""
                )
                raise _RoutingFailed(
                    edge.from_node,
                    f"json_path router on edge from {edge.from_node!r} "
                    f"matched no branch and has no default_to{why}",
                )
        elif isinstance(router, _CallableRouter):
            target = await self._router_registry.resolve(
                router.callable_id,
                context=context,
                source=source_output,
            )
        else:  # pragma: no cover -- discriminated union exhausted above.
            raise ConfigError(f"unknown router kind: {type(router).__name__}")

        if target not in self._nodes_by_id:
            raise ConfigError(
                f"router returned target {target!r} that is not a known node id"
            )
        return target
