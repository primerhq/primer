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
from typing import Any, AsyncIterator

from primer.api.registries.provider_registry import RESERVED_TOOLSET_IDS
from primer.mcp.safety import is_exposable, tool_scoped_id
from primer.model.chat import Tool
from primer.model.mcp_exposure import McpExposure
from primer.model.provider import Toolset
from primer.model.storage import OffsetPage


logger = logging.getLogger(__name__)


_USER_TOOLSET_PAGE_SIZE = 200


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ExposureDeps:
    """Bundle the two collaborators the service needs.

    Defined as a dataclass so callers (the router, tests) can construct
    the dependency bag explicitly without threading kwargs through every
    helper.
    """

    storage_provider: Any
    provider_registry: Any


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
    row = McpExposure(updated_at=_utcnow())
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
            async for tool in provider.list_tools(principal=None):
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

    # 1. Built-ins resolved from the registry without a storage row.
    for toolset_id in RESERVED_TOOLSET_IDS:
        async for pair in _emit(toolset_id):
            yield pair

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


async def _validate_allowed_tools(
    allowed: list[str], deps: ExposureDeps,
) -> None:
    """Raise :class:`ToolUnknown` or :class:`ToolNotExposable` on bad ids.

    Builds the catalogue once, then probes every requested scoped id.
    The probe consults :func:`is_exposable` so HARD_DENY + yielding +
    needs-session denials surface uniformly at write time (defence in
    depth: the dispatcher re-runs the same check on every call).
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
        })
    return out


__all__ = [
    "ExposureDeps",
    "ToolUnknown",
    "ToolNotExposable",
    "get_exposure",
    "update_exposure",
    "list_available_tools",
]
