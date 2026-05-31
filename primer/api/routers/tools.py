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

import logging

from fastapi import APIRouter, Depends

from primer.api.deps import (
    get_principal,
    get_provider_registry,
    get_storage_provider,
)
from primer.api.errors import common_responses
from primer.api.registries import ProviderRegistry
from primer.model.storage import OffsetPage

logger = logging.getLogger(__name__)


# Mirrors the built-in toolset ids declared in
# :mod:`primer.api.routers.providers` (kept in step with
# ``providers._BUILTIN_TOOLSETS``). The harness toolset is appended
# too since it's also reserved and resolved by the registry without a
# storage row.
_BUILTIN_TOOLSET_IDS: tuple[str, ...] = (
    "system",
    "workspaces",
    "search",
    "misc",
    "web",
    "harness",
)


tools_router = APIRouter(tags=["tools"])


@tools_router.get(
    "/tools/catalogue",
    summary="Flat catalogue of every tool exposed by every reachable toolset",
    responses=common_responses(500),
)
async def list_tools(
    principal: str | None = Depends(get_principal),
    registry: ProviderRegistry = Depends(get_provider_registry),
    storage_provider=Depends(get_storage_provider),
) -> dict:
    """Return ``{items: [{id, description, input_schema}, ...]}``.

    Each ``id`` is scoped as ``<toolset_id>__<tool_name>``. Toolsets
    that fail to enumerate (unreachable MCP server, missing OAuth
    consent, search subsystem not bootstrapped) are skipped silently
    so one broken provider doesn't blank the whole picker. Failures
    are logged at WARNING for operator visibility.
    """
    from primer.model.provider import Toolset

    items: list[dict] = []
    seen_ids: set[str] = set()

    async def _emit(toolset_id: str) -> None:
        try:
            provider = await registry.get_toolset(toolset_id)
        except Exception as exc:  # noqa: BLE001 -- skip broken toolset
            logger.warning(
                "list_tools: get_toolset(%r) failed: %s: %s",
                toolset_id, type(exc).__name__, exc,
            )
            return
        try:
            async for tool in provider.list_tools(principal=principal):
                scoped = f"{toolset_id}__{tool.id}"
                if scoped in seen_ids:
                    continue
                seen_ids.add(scoped)
                items.append({
                    "id": scoped,
                    "description": tool.description or "",
                    "input_schema": tool.args_schema or {},
                })
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

    # 1. Built-in toolsets (resolved by the registry without a row).
    for tid in _BUILTIN_TOOLSET_IDS:
        await _emit(tid)

    # 2. User-defined Toolset rows. Page so the catalogue scales beyond
    # the default 200-row cap.
    ts_storage = storage_provider.get_storage(Toolset)
    offset = 0
    page_size = 200
    try:
        while True:
            page = await ts_storage.list(
                OffsetPage(offset=offset, length=page_size),
            )
            for row in page.items:
                await _emit(row.id)
            if len(page.items) < page_size:
                break
            offset += page_size
    except Exception as exc:  # noqa: BLE001 -- never fail the picker
        logger.warning(
            "list_tools: iterating user toolsets failed: %s: %s",
            type(exc).__name__, exc,
        )

    return {"items": items}


__all__ = ["tools_router"]
