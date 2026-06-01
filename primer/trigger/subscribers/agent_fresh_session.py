"""agent_fresh_session subscription dispatcher — Spec §5.2.

An ``agent_fresh_session`` subscription points at one workspace +
agent pair. On fire, the dispatcher spins up a brand-new
:class:`WorkspaceSession` bound to that agent and seeds it with the
rendered payload as the initial user instruction. The factory
(:mod:`primer.workspace.session_factory`) owns the persist + claim +
auto-start steps so this dispatcher and the REST router share a
single canonical create path.

Parallelism: ``skip`` queries for any non-terminal session attributed
to this subscription via ``metadata.subscription_id`` and returns a
skip if one is in flight; ``queue`` always creates.
"""

from __future__ import annotations

import logging

from primer.model.storage import Op, OffsetPage
from primer.model.trigger import Subscription
from primer.model.workspace_session import (
    AgentSessionBinding,
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
    create_session,
)


logger = logging.getLogger(__name__)


class AgentFreshSessionDispatcher:
    """Dispatcher for ``agent_fresh_session`` subscriptions."""

    kind = "agent_fresh_session"

    async def dispatch(
        self,
        sub: Subscription,
        *,
        rendered_payload: str,
        fire_context: dict,
        fire_id: str,
        deps: DispatchDeps,
    ) -> SubscriptionDispatchResult:
        if sub.parallelism == "skip":
            skip = await _check_subscription_busy(sub, deps)
            if skip is not None:
                return skip

        factory_deps = SessionFactoryDeps(
            storage_provider=deps.storage_provider,
            claim_engine=deps.claim_engine,
            scheduler=deps.scheduler,
            workspace_registry=deps.workspace_registry,
        )
        try:
            session = await create_session(
                workspace_id=sub.config.workspace_id,
                binding=AgentSessionBinding(agent_id=sub.config.agent_id),
                initial_instructions=rendered_payload,
                graph_input=None,
                auto_start=True,
                metadata={
                    "trigger_id": sub.trigger_id,
                    "subscription_id": sub.id,
                    "fire_id": fire_id,
                    "fired_at": fire_context.get("fired_at"),
                },
                deps=factory_deps,
            )
        except Exception as exc:  # noqa: BLE001 — defensive perimeter
            return SubscriptionDispatchResult(
                ok=False,
                error_code="dispatch_failed",
                error_message=str(exc),
            )
        return SubscriptionDispatchResult(ok=True, artefact_id=session.id)


async def _check_subscription_busy(
    sub: Subscription, deps: DispatchDeps,
) -> SubscriptionDispatchResult | None:
    """Return a ``skipped`` result if any non-terminal session attributed
    to *sub* exists, otherwise ``None`` (fire normally).

    Used by both the agent_fresh and graph_fresh dispatchers so the
    busy-check semantics stay identical.
    """
    sessions = deps.storage_provider.get_storage(WorkspaceSession)
    predicate = (
        Q(WorkspaceSession)
        .where_op("metadata.subscription_id", Op.EQ, sub.id)
        .build()
    )
    page = await sessions.find(predicate, OffsetPage(offset=0, length=200))
    for s in page.items:
        if s.status != SessionStatus.ENDED:
            return SubscriptionDispatchResult(
                ok=True,
                skipped=True,
                error_code="skipped_subscription_busy",
                error_message=f"session {s.id!r} still in-flight",
            )
    return None


register("agent_fresh_session", AgentFreshSessionDispatcher())


__all__ = ["AgentFreshSessionDispatcher"]
