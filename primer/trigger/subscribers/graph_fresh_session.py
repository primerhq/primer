"""graph_fresh_session subscription dispatcher — Spec §5.3.

A ``graph_fresh_session`` subscription points at one workspace + graph
pair. On fire, the rendered payload is parsed as JSON to produce the
graph's ``graph_input``; the dispatcher then spins up a fresh
:class:`WorkspaceSession` bound to that graph through the shared
:func:`primer.workspace.session_factory.create_session` factory.

Parallelism semantics match the agent_fresh variant — ``skip``
declines firing when any non-terminal session attributed to this
subscription already exists; ``queue`` always creates.
"""

from __future__ import annotations

import json
import logging

from primer.model.principal import PrincipalRef
from primer.model.storage import Op, OffsetPage
from primer.model.trigger import Subscription
from primer.model.workspace_session import (
    GraphSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.storage.q import Q
from primer.trigger.subscribers import (
    DispatchDeps,
    SubscriptionDispatchResult,
    register,
)
from primer.workspace.session_factory import (
    SessionFactoryDeps,
    start_workspace_session,
)


logger = logging.getLogger(__name__)


class GraphFreshSessionDispatcher:
    """Dispatcher for ``graph_fresh_session`` subscriptions."""

    kind = "graph_fresh_session"

    async def dispatch(
        self,
        sub: Subscription,
        *,
        rendered_payload: str,
        fire_context: dict,
        fire_id: str,
        deps: DispatchDeps,
    ) -> SubscriptionDispatchResult:
        # The rendered payload IS the graph_input; the workspace graph
        # executor reads ``session.metadata['graph_input']`` so the
        # payload must parse as a JSON object.
        try:
            graph_input = json.loads(rendered_payload)
        except (TypeError, json.JSONDecodeError) as exc:
            return SubscriptionDispatchResult(
                ok=False,
                error_code="graph_input_invalid",
                error_message=str(exc),
            )
        if not isinstance(graph_input, dict):
            return SubscriptionDispatchResult(
                ok=False,
                error_code="graph_input_invalid",
                error_message="graph_input must be a JSON object",
            )

        if sub.parallelism == "skip":
            sessions = deps.storage_provider.get_storage(WorkspaceSession)
            predicate = (
                Q(WorkspaceSession)
                .where_op("metadata.subscription_id", Op.EQ, sub.id)
                .build()
            )
            page = await sessions.find(
                predicate, OffsetPage(offset=0, length=200),
            )
            for s in page.items:
                if s.status != SessionStatus.ENDED:
                    return SubscriptionDispatchResult(
                        ok=True,
                        skipped=True,
                        error_code="skipped_subscription_busy",
                        error_message=f"session {s.id!r} still in-flight",
                    )

        # Use start_workspace_session (NOT create_session) so the on-disk
        # graph-holder slot is allocated via the workspace backend before
        # the row auto-starts. create_session alone never allocates the
        # .state/sessions/<sid>/ directory, so the worker's
        # workspace.get_session(sid) returns None and the graph never runs
        # (the session silently ends with no transcript). This mirrors the
        # canonical REST + create_workspace_session path and requires a live
        # workspace_registry.
        if deps.workspace_registry is None:
            return SubscriptionDispatchResult(
                ok=False,
                error_code="dispatch_failed",
                error_message=(
                    "graph_fresh_session requires a workspace_registry to "
                    "allocate the on-disk session slot; the fire path did "
                    "not thread one"
                ),
            )
        factory_deps = SessionFactoryDeps(
            storage_provider=deps.storage_provider,
            claim_engine=deps.claim_engine,
            scheduler=deps.scheduler,
            workspace_registry=deps.workspace_registry,
        )
        try:
            session = await start_workspace_session(
                workspace_id=sub.config.workspace_id,
                binding=GraphSessionBinding(graph_id=sub.config.graph_id),
                initial_instructions=None,
                graph_input=graph_input,
                auto_start=True,
                metadata={
                    "trigger_id": sub.trigger_id,
                    "subscription_id": sub.id,
                    "fire_id": fire_id,
                    "fired_at": fire_context.get("fired_at"),
                    "graph_input": graph_input,
                },
                parent_session_id=None,
                initiated_by=PrincipalRef(
                    type="trigger",
                    id=sub.trigger_id,
                    display=sub.trigger_id,
                    role=None,
                    source="internal",
                ),
                deps=factory_deps,
            )
        except Exception as exc:  # noqa: BLE001 — defensive perimeter
            return SubscriptionDispatchResult(
                ok=False,
                error_code="dispatch_failed",
                error_message=str(exc),
            )
        return SubscriptionDispatchResult(ok=True, artefact_id=session.id)


register("graph_fresh_session", GraphFreshSessionDispatcher())


__all__ = ["GraphFreshSessionDispatcher"]
