"""High-level service: ``list_exposed_tools`` + ``invoke_exposed``.

The MCP server handlers (:mod:`primer.mcp.server`) call into these two
functions. Both re-run the allowlist + :func:`is_exposable` check on
every list / call so a configuration change made between two requests
reflects immediately — there is no per-process cache that could keep
serving a freshly-denied tool.

Why the re-check on every call?

The MCP ``tools/list`` cursor and the subsequent ``tools/call`` arrive
as two independent requests; an operator may flip ``enabled`` or shrink
``allowed_tools`` between them. Defence-in-depth: the same predicate
runs at PUT-time inside :mod:`primer.mcp.exposure` and again here, so a
slipping bug in either path still leaves the floor enforced.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from primer.agent.tool_manager import invoke_one
from primer.mcp.exposure import (
    ExposureDeps,
    _iter_catalogue,
    build_routing_map,
    get_exposure,
)
from primer.mcp.safety import is_exposable, tool_scoped_id
from primer.model.chat import Tool, ToolCallResult

if TYPE_CHECKING:
    from primer.model.principal import Principal


class NotExposed(Exception):
    """Raised when a requested scoped id is not currently exposed.

    Carries a structured ``reason`` so the handler layer can map it to
    the stable error-code strings listed in Spec §13 (``not_exposed``
    is the only externally-visible code; the reason is included in the
    log line for operator triage).
    """

    def __init__(self, scoped_id: str, *, reason: str | None = None) -> None:
        super().__init__(f"tool {scoped_id!r} not exposed: {reason}")
        self.scoped_id = scoped_id
        self.reason = reason


async def list_exposed_tools(
    deps: ExposureDeps,
) -> list[tuple[Tool, object]]:
    """Return ``(Tool, provider)`` for every currently-exposed tool.

    The MCP ``tools/list`` handler maps these into ``mcp_types.Tool``
    descriptors. We surface the provider alongside the tool so the
    caller does not have to round-trip through the registry a second
    time when the result list is small enough to walk twice anyway.

    Behaviour:

    * Endpoint disabled → empty list.
    * Allowlist member that fails :func:`is_exposable` → silently
      dropped (the floor wins over the operator's allowlist; this also
      defends against a future drift where the exposability rules grow
      stricter than what was allowed at the time of PUT).
    * Catalogue tools that are NOT in the allowlist → silently dropped.
    """
    exposure = await get_exposure(deps)
    if not exposure.enabled:
        return []
    allowed = set(exposure.allowed_tools)
    out: list[tuple[Tool, object]] = []
    async for tool, provider in _iter_catalogue(deps):
        scoped = tool_scoped_id(tool)
        if scoped not in allowed:
            continue
        ok, _reason = is_exposable(tool, provider=provider)
        if not ok:
            continue
        out.append((tool, provider))
    return out


async def _enforce_approval_gate(
    *,
    scoped_id: str,
    toolset_id: str,
    bare_name: str,
    arguments: dict,
    principal: str | None,
    deps: ExposureDeps,
) -> None:
    """Refuse the call if the tool's effective approval policy is required.

    Reuses the agent/session approval engine
    (:class:`primer.agent.approval.ApprovalResolver` +
    :func:`evaluate_approval_gate`) so MCP can never run a tool the
    operator gated. Raises :class:`NotExposed` with
    ``reason="approval_required"`` when the verdict requires approval.

    The resolver is normally wired by the lifespan; when it is absent
    (``deps.approval_resolver is None`` -- only in narrow non-dispatch
    test setups) we skip, mirroring the agent path which no-ops when no
    resolver is configured.
    """
    from datetime import datetime, timezone

    from primer.agent.approval import (
        ApprovalContext,
        evaluate_approval_gate,
    )

    resolver = getattr(deps, "approval_resolver", None)
    if resolver is None:
        return
    policy = await resolver.find(toolset_id=toolset_id, tool_name=bare_name)
    if policy is None or not policy.enabled:
        return
    ctx = ApprovalContext(
        tool_name=bare_name,
        toolset_id=toolset_id,
        arguments=arguments or {},
        agent_id=None,
        session_id=None,
        chat_id=None,
        requested_at=datetime.now(timezone.utc),
    )
    verdict = await evaluate_approval_gate(
        policy=policy,
        context=ctx,
        provider_registry=deps.provider_registry,
    )
    if verdict.required:
        raise NotExposed(scoped_id, reason="approval_required")


async def invoke_exposed(
    *,
    scoped_id: str,
    arguments: dict,
    principal: str | None,
    deps: ExposureDeps,
    actor: "Principal | None" = None,
) -> ToolCallResult:
    """Dispatch a ``tools/call`` to the owning provider.

    Re-checks the live ``McpExposure`` allowlist and the safety
    predicate on every call. Resolves ``scoped_id`` to
    ``(toolset_id, bare_name)`` via the precomputed routing map built
    by :func:`primer.mcp.exposure.build_routing_map`: the keys are the
    exact scoped ids the catalogue advertises, so the inverse is exact
    even when a harness-deployed toolset id contains ``__`` (e.g.
    ``acme__ts``) or a built-in bare name does (e.g. ``harness__list``);
    a naive first- or last-``__`` split mis-resolves one or the other.
    Then forwards to :func:`primer.agent.tool_manager.invoke_one` so
    OTel + Prometheus instrumentation stays unified with the
    agent-driven path.

    Approval gate
    -------------
    A tool whose effective :class:`ToolApprovalPolicy` resolves to
    ``required`` is REFUSED here (``reason="approval_required"``) rather
    than dispatched: MCP v1 has no park/resume surface to collect a human
    (or LLM-judge / Rego) decision, so running it unconditionally would
    silently bypass the very gate the operator configured. We reuse the
    same :class:`primer.agent.approval.ApprovalResolver` +
    :func:`evaluate_approval_gate` the agent/session path uses, and the
    engine fails closed, so a broken judge/policy still refuses rather
    than leaks. The check is re-run on every call (like the allowlist /
    exposability checks) so a policy edit takes effect immediately.

    Raises
    ------
    NotExposed
        Endpoint disabled, scoped id not in allowlist, scoped id
        malformed, provider missing, target tool missing from the
        provider's catalogue, :func:`is_exposable` rejected the tool, or
        the tool's effective approval policy is ``required``
        (``reason="approval_required"``), or a reserved ``system``
        mutation tool (``create_``/``update_``/``delete_``) was invoked
        by a non-admin actor (``reason="forbidden_role"``). The ``reason``
        attribute differentiates the cause.
    """
    exposure = await get_exposure(deps)
    if not exposure.enabled or scoped_id not in set(exposure.allowed_tools):
        raise NotExposed(scoped_id, reason="not_in_allowlist")
    if "__" not in scoped_id:
        raise NotExposed(scoped_id, reason="malformed_id")
    routing = await build_routing_map(deps)
    entry = routing.get(scoped_id)
    if entry is not None:
        toolset_id, bare_name = entry
    else:
        # Not in the live catalogue (toolset gone or tool removed since
        # the allowlist was written). Fall back to a first-``__`` split
        # purely to preserve the provider_missing / tool_missing reason
        # distinction below; the resolved id is never dispatched because
        # the provider/tool lookup that follows fails for it by design.
        toolset_id, bare_name = scoped_id.split("__", 1)
    # RBAC floor: reserved ``system``-toolset mutation tools
    # (``create_``/``update_``/``delete_``) are admin-only even when the
    # operator allow-listed them. A system-type Principal (the
    # auth-disabled bypass) counts as admin; any user/api_token below
    # admin — or a missing actor — is refused here, before the provider
    # is ever touched. ``is_exposable`` is deliberately left role-free.
    if toolset_id == "system" and bare_name.startswith(
        ("create_", "update_", "delete_")
    ):
        is_admin = actor is not None and (
            actor.type == "system" or actor.role == "admin"
        )
        if not is_admin:
            raise NotExposed(scoped_id, reason="forbidden_role")
    provider = await deps.provider_registry.get_toolset(toolset_id)
    if provider is None:
        raise NotExposed(scoped_id, reason="provider_missing")
    tool: Tool | None = None
    async for candidate in provider.list_tools(principal=principal):
        if candidate.id == bare_name:
            tool = candidate
            break
    if tool is None:
        raise NotExposed(scoped_id, reason="tool_missing")
    ok, reason = is_exposable(tool, provider=provider)
    if not ok:
        raise NotExposed(scoped_id, reason=reason)
    await _enforce_approval_gate(
        scoped_id=scoped_id,
        toolset_id=toolset_id,
        bare_name=bare_name,
        arguments=arguments,
        principal=principal,
        deps=deps,
    )
    return await invoke_one(
        provider=provider,
        tool_name=bare_name,
        arguments=arguments,
        principal=principal,
    )


__all__ = ["NotExposed", "list_exposed_tools", "invoke_exposed"]
