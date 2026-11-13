"""GET /v1/tools/catalogue — flat tool catalogue for the graph editor.

Spec B §3.4. Returns a flat list of ``{id, description, input_schema}``
records — one per tool exposed by any reachable toolset provider on the
platform (built-in + user-defined). Each ``id`` is the scoped form
``<toolset_id>__<tool_name>`` so the Phase 9 ToolCall picker can route
invocations back to the right toolset.

Distinct from the pre-existing ``GET /v1/tools`` endpoint in
:mod:`primer.api.routers.providers`, which returns the same data
grouped by toolset for the operator console's per-toolset views. The
flat shape here is what Spec B §3.4 contracts, and it lives at
``/tools/catalogue`` so both endpoints can coexist without breaking
existing UI consumers.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter

from fastapi import APIRouter, Depends, Request

from primer.access.person import person_from_request
from primer.access.policy import catalogue_predicate, level_of
from primer.api.deps import (
    get_principal,
    get_provider_registry,
    get_storage_provider,
)
from primer.api.errors import common_responses
from primer.api.registries import ProviderRegistry
from primer.api.route_tools import admin_operation_keys, coverage_projection, route_tool
from primer.api.routers.providers import _CATALOGUE_CONCURRENCY, _all_builtin_toolsets
from primer.knowledge.wiki.reference import reference_path
from primer.mcp.exposure import ExposureDeps, get_exposure
from primer.mcp.safety import is_exposable
from primer.model.chat import tool_catalogue_flags
from primer.model.storage import OffsetPage
from primer.toolset.tool_catalog import default_gate

logger = logging.getLogger(__name__)


# The built-in toolset ids, taken from the ONE list in
# :mod:`primer.api.routers.providers` (``_BUILTIN_TOOLSETS``, which also feeds
# ``GET /v1/tools`` and the built-in toolset cards). This module used to keep a
# hand-copied tuple "in step" with it, and the copy lacked ``trigger``: the flat
# catalogue (and the Tools page built on it) omitted that toolset's tools while
# every picker listed them.
tools_router = APIRouter(tags=["tools"])

# Per-toolset ceiling for catalogue enumeration. A toolset backed by an
# unreachable MCP server blocks rather than raising, so the skip-broken-
# provider contract needs a clock behind it, not just except handlers.
_ENUMERATE_TIMEOUT_S = 10.0


@tools_router.get(
    "/tools/catalogue",
    summary="Flat catalogue of every tool exposed by every reachable toolset",
    responses=common_responses(500),
)
@route_tool(scoped_id="tools__list_tools")
async def list_tools(
    request: Request,
    principal: str | None = Depends(get_principal),
    registry: ProviderRegistry = Depends(get_provider_registry),
    storage_provider=Depends(get_storage_provider),
) -> dict:
    """Return ordered picker items plus caller-visible coverage facts.

    Each ``id`` is scoped as ``<toolset_id>__<tool_name>``. Toolsets
    that fail to enumerate (unreachable MCP server, missing OAuth
    consent, search subsystem not bootstrapped) are skipped silently
    so one broken provider doesn't blank the whole picker. Failures
    are logged at WARNING for operator visibility.

    New metadata is additive: admin REST mappings and reviewed skips are
    role-filtered independently of the existing item list. Exposure and
    operation totals are admin-only; approval describes the shipped default.
    """
    from primer.model.provider import Toolset

    person = person_from_request(request)
    projection = coverage_projection(request.app)
    admin_keys = admin_operation_keys(request.app)
    operations = {
        scoped: {"operation_id": row.operation_id, "method": row.method, "path": row.path}
        for row in projection
        if person.is_admin or (row.method, row.path) not in admin_keys
        for scoped in row.tool_ids
    }
    hidden_operations = {
        scoped for row in projection
        if not person.is_admin and (row.method, row.path) in admin_keys
        for scoped in row.tool_ids
    }
    generated_ids = request.app.state.generated_route_toolsets.providers
    exposure = await get_exposure(ExposureDeps(storage_provider, registry)) if person.is_admin else None
    allowed = frozenset(exposure.allowed_tools) if exposure is not None else frozenset()
    items: list[dict] = []
    seen_ids: set[str] = set()

    # The per-row work (get_toolset + enumerate) is independent per row, so
    # the rows overlap under the bound instead of serializing one toolset
    # behind the next. A row answers its OWN list; the route merges the lists
    # into ``items``/``seen_ids`` in row order (builtins first, then user rows
    # in page order), so the first row to expose a scoped id still wins.
    sem = asyncio.Semaphore(_CATALOGUE_CONCURRENCY)

    async def _tools_for(
        toolset_id: str, only: frozenset[str] | None = None, *, builtin: bool = False,
    ) -> list[dict]:
        own: list[dict] = []
        try:
            async with sem:
                async with asyncio.timeout(_ENUMERATE_TIMEOUT_S):
                    provider = await registry.get_toolset(toolset_id)
                    async for tool in provider.list_tools(principal=principal):
                        if only is not None and tool.id not in only:
                            continue  # a row the caller cannot read shows only its public tools
                        scoped = f"{toolset_id}__{tool.id}"
                        item = {
                            "id": scoped,
                            "description": tool.description or "",
                            "input_schema": tool.args_schema or {},
                            # y/w/r/n capability badges, excluded from Tool's own
                            # wire serialisation (chat.py) since they must never
                            # reach the LLM-facing tool schema;
                            # tool_catalogue_flags() is the one seam every
                            # "list tools" route re-adds them from.
                            **tool_catalogue_flags(tool),
                            "origin": "generated" if toolset_id in generated_ids else "curated" if builtin else "user",
                            "reference_path": reference_path(toolset_id, tool.id),
                            "default_approval": default_gate(toolset_id, tool.id),
                            "exposure": None,
                        }
                        if scoped not in hidden_operations:
                            item["operation"] = operations.get(scoped)
                        if exposure is not None:
                            ok, reason = is_exposable(tool, provider=provider)
                            item["exposure"] = {
                                "exposable": ok, "reason": reason,
                                "currently_allowed": scoped in allowed, "mode": exposure.mode,
                            }
                        own.append(item)
        except TimeoutError:
            # An unreachable MCP server accepts the connection and then
            # never answers; it does not raise. Without this bound one
            # such toolset stalls the whole catalogue forever.
            logger.warning(
                "list_tools: enumerate %r timed out after %ss; skipped",
                toolset_id, _ENUMERATE_TIMEOUT_S,
            )
        except BaseExceptionGroup as group:
            logger.warning(
                "list_tools: enumerate %r raised group: %s",
                toolset_id, group,
            )
        except Exception as exc:  # noqa: BLE001 -- skip broken toolset
            logger.warning(
                "list_tools: enumerate %r failed: %s: %s",
                toolset_id, type(exc).__name__, exc,
            )
        return own

    def _merge(tools: list[dict]) -> None:
        for t in tools:
            if t["id"] in seen_ids:
                continue
            seen_ids.add(t["id"])
            items.append(t)

    # 1. Built-in toolsets (resolved by the registry without a row).
    from primer.api.routers.providers import _canvas_tools_on

    canvas_on = await _canvas_tools_on(storage_provider)
    builtin_ids = [
        tid for tid in (spec["id"] for spec in _all_builtin_toolsets())
        if not (tid == "canvas" and not canvas_on)
    ]
    for tools in await asyncio.gather(*(_tools_for(tid, builtin=True) for tid in builtin_ids)):
        _merge(tools)

    # 2. User-defined Toolset rows. Page so the catalogue scales beyond
    # the default 200-row cap. Rows the caller may not read stay out (spec
    # anti-goal 4: filter in storage); admins get a None predicate and keep
    # the unfiltered list.
    access_pred = catalogue_predicate(person)
    ts_storage = storage_provider.get_storage(Toolset)
    offset = 0
    page_size = 200
    try:
        while True:
            page = await ts_storage.find(
                access_pred,
                OffsetPage(offset=offset, length=page_size, want_total=False),
            )
            onlys = [
                None if level_of(person, row.access) is not None
                else frozenset(row.access.public_tools)
                for row in page.items
            ]
            for tools in await asyncio.gather(
                *(_tools_for(row.id, only) for row, only in zip(page.items, onlys))
            ):
                _merge(tools)
            if len(page.items) < page_size:
                break
            offset += page_size
    except Exception as exc:  # noqa: BLE001 -- never fail the picker
        logger.warning(
            "list_tools: iterating user toolsets failed: %s: %s",
            type(exc).__name__, exc,
        )

    result = {
        "items": items,
        "skips": [
            {
                "operation_id": row.operation_id, "method": row.method, "path": row.path,
                "skip_code": row.skip_code, "reason": (row.skip_code or "").replace("_", " "),
            }
            for row in projection if row.state == "skip"
            and (person.is_admin or (row.method, row.path) not in admin_keys)
        ],
    }
    if person.is_admin:
        counts = Counter(row.state for row in projection)
        result["coverage_counts"] = {"operations": len(projection), **counts}
    return result


__all__ = ["tools_router"]
