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

from primer.agent.tool_manager import invoke_one
from primer.mcp.exposure import (
    ExposureDeps,
    _iter_catalogue,
    build_routing_map,
    get_exposure,
)
from primer.mcp.safety import is_exposable, tool_scoped_id
from primer.model.chat import Tool, ToolCallResult


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


async def invoke_exposed(
    *,
    scoped_id: str,
    arguments: dict,
    principal: str | None,
    deps: ExposureDeps,
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

    Raises
    ------
    NotExposed
        Endpoint disabled, scoped id not in allowlist, scoped id
        malformed, provider missing, target tool missing from the
        provider's catalogue, or :func:`is_exposable` rejected the
        tool. The ``reason`` attribute differentiates the cause.
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
    return await invoke_one(
        provider=provider,
        tool_name=bare_name,
        arguments=arguments,
        principal=principal,
    )


__all__ = ["NotExposed", "list_exposed_tools", "invoke_exposed"]
