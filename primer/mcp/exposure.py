"""Service layer for :class:`McpExposure` CRUD + tool availability enrichment.

Implements the spec §6 service contract: callers (the REST router in
Phase 6 and the dispatch layer in Phase 4) interact with the singleton
row exclusively through this module, never poking storage directly.

Responsibilities:

* :func:`get_exposure` -- lazy-create + return the singleton row.
* :func:`update_exposure` -- mutate ``enabled`` / ``allowed_tools`` with
  audit stamping and full validation of each scoped id against the live
  toolset catalogue.
* :func:`list_available_tools` -- enrich every catalogue tool with
  exposability + currently-allowed flags so the UI table can render
  green/red dots without a second round-trip.

The catalogue iteration mirrors :mod:`primer.api.routers.tools` (built-in
toolset ids first, then user-defined Toolset rows paginated) so the two
endpoints stay in step. Toolsets that fail to enumerate are logged and
skipped, never propagated -- a single broken provider must not blank the
allowlist UI.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from collections.abc import AsyncIterator

from primer.api.registries.provider_registry import RESERVED_TOOLSET_IDS
from primer.common.bounded_jsonschema import MAX_INSTANCE_BYTES
from primer.mcp.safety import is_exposable, tool_scoped_id
from primer.model.chat import Tool, tool_catalogue_flags
from primer.model.mcp_exposure import McpExposure
from primer.model.provider import Toolset
from primer.model.storage import OffsetPage


logger = logging.getLogger(__name__)


_USER_TOOLSET_PAGE_SIZE = 200


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ExposureDeps:
    """Bundle the collaborators the service needs.

    Defined as a dataclass so callers (the router, tests) can construct
    the dependency bag explicitly without threading kwargs through every
    helper.

    ``approval_resolver`` is the singleton
    :class:`primer.agent.approval.ApprovalResolver` wired in the
    lifespan. The dispatch path (:func:`invoke_exposed`) consults it to
    refuse approval-gated tools, which MCP v1 cannot park for a human
    decision. It is optional (``None``) only for the few callers that
    never dispatch (the CRUD router, exposure-listing tests); the
    production lifespan always supplies it.

    ``max_body_bytes`` is the app's request-body cap
    (``limits.max_body_bytes``) the MCP mount was installed with: the
    argument-validation size bound of the meta-tools, so a body the
    body-cap middleware admits (up to this cap) validates and dispatches
    instead of being refused at the historical 1 MiB default.
    """

    storage_provider: Any
    provider_registry: Any
    approval_resolver: Any = None
    # Optional semantic search seam. It receives ``(query, limit)`` and
    # returns stable wiki reference paths. Discovery always has a lexical
    # fallback, so boot and MCP never depend on an index being configured.
    semantic_search: Any = None
    max_body_bytes: int = MAX_INSTANCE_BYTES


class ToolUnknown(Exception):
    """Raised when ``allowed_tools`` contains a scoped id no toolset emits."""

    def __init__(self, scoped_id: str) -> None:
        super().__init__(f"unknown tool {scoped_id!r}")
        self.scoped_id = scoped_id


class ToolNotExposable(Exception):
    """Raised when an allowed-tool id is real but blocked by :func:`is_exposable`."""

    def __init__(self, scoped_id: str, reason: str) -> None:
        super().__init__(f"tool {scoped_id!r} not exposable: {reason}")
        self.scoped_id = scoped_id
        self.reason = reason


@dataclass(frozen=True)
class AuthorizedTool:
    """One reserved tool the current MCP caller may discover and call."""

    tool: Tool
    provider: Any
    scoped_id: str
    required_role: str


def _person_for_actor(actor: Any) -> Any:
    """Derive the access-control Person for an MCP request actor."""
    from primer.access.person import Person

    if actor is None:
        return Person(None, "restricted")
    if actor.type == "system":
        return Person(None, "system")
    return Person(actor.user_id, actor.role or "user")


async def statically_required(deps: ExposureDeps, *, toolset_id: str, tool_name: str) -> bool:
    """Return True when the tool's effective approval policy is the ``required`` type.

    This is the one approval-gate decision discovery can make without a
    model call: a ``required`` policy refuses every call, so its tools are
    withheld from the catalogue (advertising one could only answer
    tool_unavailable). An LLM judge or a Rego policy is deliberately not
    covered: it decides per call in the dispatch path, where a model call
    is allowed. With no resolver wired (narrow non-dispatch test setups)
    nothing is withheld, mirroring the dispatch no-op; the resolver lookup
    itself is cached.
    """
    from primer.model.tool_approval import RequiredApprovalConfig

    resolver = getattr(deps, "approval_resolver", None)
    if resolver is None:
        return False
    policy = await resolver.find(toolset_id=toolset_id, tool_name=tool_name)
    return (
        policy is not None
        and policy.enabled
        and isinstance(policy.approval, RequiredApprovalConfig)
    )


async def _withhold_reason(
    deps: ExposureDeps,
    *,
    exposure: McpExposure,
    actor: Any,
    scoped: str,
    tool: Tool,
    provider: Any,
    required_role: str,
) -> str | None:
    """Return why ``tool`` is withheld from ``actor``'s catalogue, or ``None``.

    Runs the per-tool discovery checks in :func:`authorized_catalogue`'s
    order (allowlist, exposable, role floor, static approval gate) and
    reports the FIRST check that withholds the tool: that outermost check
    is why the caller never sees it. ``None`` means every check passed.
    Only the static gate is a reason worth recording (``"approval_required"``);
    the other withholdings return their check name, and
    :func:`hidden_rejection_reason` folds them back to ``None``.
    """
    from primer.authz import _role_allows

    if exposure.mode == "allowlist" and scoped not in exposure.allowed_tools:
        return "allowlist"
    ok, _reason = is_exposable(tool, provider=provider)
    if not ok:
        return "not_exposable"
    if not _role_allows(actor, required_role):
        return "role_floor"
    if await statically_required(deps, toolset_id=tool.toolset_id, tool_name=tool.id):
        return "approval_required"
    return None


async def hidden_rejection_reason(
    deps: ExposureDeps,
    *,
    actor: Any,
    principal: str | None,
    api_token_scopes: list[str] | None,
    scoped_id: str,
) -> str | None:
    """Return the audit reason for a target the catalogue does not show.

    ``call_tool`` answers the fixed ``tool_unavailable`` envelope for any
    target it cannot resolve. The audit trail records the cause when the
    cause is the static approval gate: a ``required``-gated tool clears the
    caller's allowlist, exposable and role-floor checks but is withheld by
    the gate, and the refusal is recorded as ``approval_required`` (the
    contract in ``skills/using-primer-over-mcp.md``). A target that is
    unknown, or withheld by any earlier check (exposure off, no mcp scope,
    allowlist, exposable, role floor), has no recoverable reason and
    returns ``None``.
    """
    from primer.model.api_token import SCOPE_MCP

    exposure = await get_exposure(deps)
    if not exposure.enabled:
        return None
    if api_token_scopes is not None and SCOPE_MCP not in api_token_scopes:
        return None
    # An enumeration failure means "no reason known" (the caller answers tool_unavailable), as _iter_catalogue skips a broken toolset.
    try:
        route = (await build_routing_map(deps, reserved_only=True)).get(scoped_id)
        if route is None:
            return None
        toolset_id, tool_name = route
        provider = await deps.provider_registry.get_toolset(toolset_id)
        tool = None
        async for item in provider.list_tools(principal=principal):
            if item.id == tool_name:
                tool = item
                break
    except Exception as exc:  # noqa: BLE001 -- a broken toolset is not resolvable
        logger.warning(
            "mcp_exposure: hidden-target lookup for %r failed: %s: %s",
            scoped_id, type(exc).__name__, exc,
        )
        return None
    if tool is None:
        return None
    reason = await _withhold_reason(
        deps, exposure=exposure, actor=actor, scoped=scoped_id,
        tool=tool, provider=provider,
        required_role=provider.required_role(tool.id),
    )
    # Only the static gate is an audit reason; the other withholdings
    # (and a tool that passed every check) stay reasonless.
    return reason if reason == "approval_required" else None


async def authorized_catalogue(
    deps: ExposureDeps,
    *,
    actor: Any,
    principal: str | None,
    api_token_scopes: list[str] | None,
) -> list[AuthorizedTool]:
    """Return the reserved catalogue visible to one MCP caller.

    This is the single discovery authorization seam. It applies the global
    switch, legacy allowlist or RBAC mode, MCP bearer scope, exposability,
    role floor, the approval gate's static rule (a tool whose effective
    policy is the ``required`` type is withheld: it refuses every call, so
    advertising it could only answer tool_unavailable; LLM and Rego policies
    are NOT evaluated here -- a listing must never trigger a model call --
    and the dispatch path keeps deciding them per call), and per-person
    toolset use before an identifier is projected.
    User-created toolsets are never walked.
    """
    from primer.access.tools import toolset_use
    from primer.model.api_token import SCOPE_MCP

    exposure = await get_exposure(deps)
    if not exposure.enabled:
        return []
    if api_token_scopes is not None and SCOPE_MCP not in api_token_scopes:
        return []

    candidates: list[tuple[Tool, Any, str, str]] = []
    async for tool, provider in _iter_catalogue(
        deps,
        reserved_only=True,
        principal=principal,
    ):
        scoped = tool_scoped_id(tool)
        required_role = provider.required_role(tool.id)
        if await _withhold_reason(
            deps, exposure=exposure, actor=actor, scoped=scoped,
            tool=tool, provider=provider, required_role=required_role,
        ):
            continue
        candidates.append((tool, provider, scoped, required_role))

    uses = await toolset_use(
        deps.storage_provider,
        _person_for_actor(actor),
        [tool.toolset_id for tool, _provider, _scoped, _role in candidates],
    )
    out = [
        AuthorizedTool(tool, provider, scoped, required_role)
        for tool, provider, scoped, required_role in candidates
        if uses.get(tool.toolset_id) is None
        or tool.id in uses.get(tool.toolset_id, frozenset())
    ]
    return sorted(out, key=lambda item: item.scoped_id)


async def get_exposure(deps: ExposureDeps) -> McpExposure:
    """Return the singleton row, lazily creating it on first call.

    A fresh install has no row in storage; the first GET / PUT creates
    one with ``enabled=False`` and ``allowed_tools=[]`` -- the safe
    default. The create is racy against parallel workers, so a
    ``ConflictError`` falls back to reading the row the other worker
    just wrote.
    """
    storage = deps.storage_provider.get_storage(McpExposure)
    row = await storage.get("singleton")
    if row is not None:
        return row
    row = McpExposure(mode="rbac", updated_at=_utcnow())
    try:
        await storage.create(row)
    except Exception:  # noqa: BLE001 -- treat any create failure as a race
        existing = await storage.get("singleton")
        if existing is not None:
            return existing
        raise
    return row


async def _iter_catalogue(
    deps: ExposureDeps,
    *,
    reserved_only: bool = False,
    principal: str | None = None,
) -> AsyncIterator[tuple[Tool, Any]]:
    """Yield ``(Tool, ToolsetProvider)`` for every reachable tool.

    Mirrors :func:`primer.api.routers.tools.list_tools`: built-in
    toolsets (resolved by the registry without a storage row) come
    first, then the user-defined :class:`Toolset` rows are paginated.
    Duplicates across providers are deduped by scoped id so the first
    emitter wins, matching the catalogue endpoint's behaviour.

    Toolsets that fail to resolve or enumerate are logged at WARNING
    and skipped -- one broken provider must not bring down the
    exposure UI.

    ``reserved_only`` stops after the built-ins. primer's own MCP server
    (tools/list, tools/call) walks only those: a user toolset's tools are
    never exposable (``not_system_toolset``), and enumerating one starts its
    external server, which can hang /v1/mcp (W10).
    """
    registry = deps.provider_registry
    storage_provider = deps.storage_provider
    seen: set[str] = set()

    async def _emit(toolset_id: str) -> AsyncIterator[tuple[Tool, Any]]:
        try:
            provider = await registry.get_toolset(toolset_id)
        except Exception as exc:  # noqa: BLE001 -- skip broken toolset
            logger.warning(
                "mcp_exposure: get_toolset(%r) failed: %s: %s",
                toolset_id, type(exc).__name__, exc,
            )
            return
        if provider is None:
            return
        try:
            async for tool in provider.list_tools(principal=principal):
                key = tool_scoped_id(tool)
                if key in seen:
                    continue
                seen.add(key)
                yield tool, provider
        except BaseExceptionGroup as group:
            logger.warning(
                "mcp_exposure: enumerate %r raised group: %s",
                toolset_id, group,
            )
        except Exception as exc:  # noqa: BLE001 -- skip broken toolset
            logger.warning(
                "mcp_exposure: enumerate %r failed: %s: %s",
                toolset_id, type(exc).__name__, exc,
            )

    # 1. Built-ins resolved from the registry without a storage row. One the install has switched off on purpose (the canvas while
    # canvas.agent_tools_enabled is off) is not a broken toolset: it is left out without the WARNING every tools/list would log.
    try:
        off = await registry.toolsets_off_by_design()
    except Exception:  # noqa: BLE001 -- unreadable settings: try every built-in, as before
        off = frozenset()
    for toolset_id in RESERVED_TOOLSET_IDS:
        if toolset_id in off:
            continue
        async for pair in _emit(toolset_id):
            yield pair
    if reserved_only:
        return

    # 2. User-defined Toolset rows.  Paged so the catalogue scales
    # beyond the default storage cap.
    ts_storage = storage_provider.get_storage(Toolset)
    offset = 0
    try:
        while True:
            page = await ts_storage.list(
                OffsetPage(offset=offset, length=_USER_TOOLSET_PAGE_SIZE),
            )
            for row in page.items:
                async for pair in _emit(row.id):
                    yield pair
            if len(page.items) < _USER_TOOLSET_PAGE_SIZE:
                break
            offset += _USER_TOOLSET_PAGE_SIZE
    except Exception as exc:  # noqa: BLE001 -- never fail the picker
        logger.warning(
            "mcp_exposure: iterating user toolsets failed: %s: %s",
            type(exc).__name__, exc,
        )


# Process-level cache for the dispatch routing map, keyed on the
# McpExposure singleton's ``updated_at`` stamp. The map is otherwise
# rebuilt on every ``tools/call`` (a full catalogue enumeration). Caching
# it is safe because a tool is only DISPATCHABLE when it is in the
# allowlist, and the allowlist can only change through
# :func:`update_exposure`, which bumps ``updated_at`` -- so any change
# that could make a new scoped id routable also invalidates this cache.
# (A toolset added WITHOUT an exposure edit is not yet in the allowlist,
# so its absence from a stale map never affects a real dispatch.)
_ROUTING_CACHE: dict[str, tuple[datetime, dict[str, tuple[str, str]]]] = {}


def _routing_cache_key(deps: ExposureDeps, reserved_only: bool) -> str:
    """Key the cache per storage provider so two providers (e.g. distinct
    test instances) never share a routing map, and per catalogue walked."""
    return f"{id(deps.storage_provider)}:{reserved_only}"


def _clear_routing_cache() -> None:
    """Drop the cached routing maps (test seam)."""
    _ROUTING_CACHE.clear()


async def build_routing_map(
    deps: ExposureDeps,
    *,
    use_cache: bool = True,
    reserved_only: bool = False,
) -> dict[str, tuple[str, str]]:
    """Return ``{scoped_id: (toolset_id, bare_tool_name)}`` for the catalogue.

    The keys are the exact scoped ids :func:`list_exposed_tools` advertises
    (computed via :func:`tool_scoped_id`), so the inverse mapping back to
    ``(toolset_id, bare_name)`` is exact regardless of how many ``__``
    separators a scoped id contains. This is the authoritative resolver the
    dispatch path uses instead of string-splitting: a harness-deployed
    toolset whose id itself contains ``__`` (e.g. ``acme__ts``) resolves
    correctly, where a first- or last-``__`` split cannot.

    The result is memoized per storage provider and keyed on the
    :class:`McpExposure` row's ``updated_at`` stamp: a hit returns the cached
    map without re-enumerating the catalogue; a miss (first call, or the
    stamp moved because the operator edited the allowlist) rebuilds it. Pass
    ``use_cache=False`` to force a fresh build (used by the validation path,
    which must see in-flight catalogue changes). ``reserved_only`` maps the
    built-in toolsets only (see :func:`_iter_catalogue`).
    """
    if use_cache:
        exposure = await get_exposure(deps)
        key = _routing_cache_key(deps, reserved_only)
        cached = _ROUTING_CACHE.get(key)
        if cached is not None and cached[0] == exposure.updated_at:
            return cached[1]

    routing: dict[str, tuple[str, str]] = {}
    async for tool, _provider in _iter_catalogue(deps, reserved_only=reserved_only):
        routing[tool_scoped_id(tool)] = (tool.toolset_id, tool.id)

    if use_cache:
        _ROUTING_CACHE[_routing_cache_key(deps, reserved_only)] = (exposure.updated_at, routing)
    return routing


async def _validate_allowed_tools(
    allowed: list[str], deps: ExposureDeps,
) -> None:
    """Raise :class:`ToolUnknown` or :class:`ToolNotExposable` on bad ids.

    Builds the catalogue once, then probes every requested scoped id.
    The probe consults :func:`is_exposable` so the runtime constraints
    (yielding tools, workspace tools needing an AgentSession) surface
    uniformly at write time. Defence in depth: the dispatcher re-runs
    the same check on every call.
    """
    catalogue: dict[str, tuple[Tool, Any]] = {}
    async for tool, provider in _iter_catalogue(deps):
        catalogue[tool_scoped_id(tool)] = (tool, provider)
    for scoped in allowed:
        pair = catalogue.get(scoped)
        if pair is None:
            raise ToolUnknown(scoped)
        tool, provider = pair
        ok, reason = is_exposable(tool, provider=provider)
        if not ok:
            raise ToolNotExposable(scoped, reason or "unknown")


async def update_exposure(
    *,
    enabled: bool | None,
    mode: str | None = None,
    allowed_tools: list[str] | None,
    updated_by: str | None,
    deps: ExposureDeps,
) -> McpExposure:
    """Mutate the singleton row and persist it.

    ``None`` fields mean "leave alone" so the PATCH-shaped router body
    maps cleanly. ``allowed_tools`` is deduped + sorted so storage stays
    canonical (UI diffs and audit logs stay sensible). Every mutation
    bumps ``updated_at`` and stamps ``updated_by`` even if the caller
    only flipped ``enabled``.
    """
    row = await get_exposure(deps)
    if enabled is not None:
        row = row.model_copy(update={"enabled": enabled})
    if mode is not None:
        if mode not in ("allowlist", "rbac"):
            raise ValueError("mode must be 'allowlist' or 'rbac'")
        row = row.model_copy(update={"mode": mode})
    if allowed_tools is not None:
        await _validate_allowed_tools(allowed_tools, deps)
        row = row.model_copy(update={
            "allowed_tools": sorted(set(allowed_tools)),
        })
    row = row.model_copy(update={
        "updated_at": _utcnow(),
        "updated_by": updated_by,
    })
    storage = deps.storage_provider.get_storage(McpExposure)
    await storage.update(row)
    return row


async def list_available_tools(deps: ExposureDeps) -> list[dict]:
    """Return the UI table: every catalogue tool with availability flags.

    Each dict carries:

    * ``scoped_id`` -- wire-level ``toolset_id__tool_id`` identifier.
    * ``toolset_id`` -- the owning toolset for grouping in the UI.
    * ``description`` -- the tool's free-form description (may be empty).
    * ``exposable`` -- :func:`is_exposable` verdict.
    * ``reason`` -- denial reason string (``None`` when ``exposable``).
    * ``currently_allowed`` -- membership in the live allowlist.
    * ``yields`` / ``requires_workspace`` / ``tool_class`` / ``required_role`` /
      ``interruptible`` -- the same y/w/r/n capability badges (and the
      ``interruptible`` flag a Stop consults) every other "list tools"
      route re-adds via :func:`~primer.model.chat.tool_catalogue_flags`;
      this was the one catalogue consumer that omitted them (platform
      wave P2, #28) -- an operator building the MCP allowlist could not
      tell a workspace-only or yielding tool apart from a plain one
      before deciding to expose it.

    The shape feeds the Phase 8 console table directly.
    """
    exposure = await get_exposure(deps)
    allowed_set = set(exposure.allowed_tools)
    out: list[dict] = []
    async for tool, provider in _iter_catalogue(deps):
        ok, reason = is_exposable(tool, provider=provider)
        scoped = tool_scoped_id(tool)
        out.append({
            "scoped_id": scoped,
            "toolset_id": tool.toolset_id,
            "description": tool.description or "",
            "exposable": ok,
            "reason": reason,
            "currently_allowed": scoped in allowed_set,
            **tool_catalogue_flags(tool),
        })
    return out


__all__ = [
    "AuthorizedTool",
    "ExposureDeps",
    "ToolUnknown",
    "ToolNotExposable",
    "get_exposure",
    "update_exposure",
    "list_available_tools",
    "build_routing_map",
    "authorized_catalogue",
]
