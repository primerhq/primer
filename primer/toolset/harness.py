"""``harness`` internal toolset — mirrors the Harness REST API.

Exposes 9 tools that an agent can use to manage harnesses in-process
without going through HTTP:

* ``harness__list``           — list with optional slug/status filters.
* ``harness__get``            — fetch one row by id.
* ``harness__register``       — create a DRAFT harness.
* ``harness__update``         — update mutable metadata.
* ``harness__update_overrides`` — validate + store overrides.
* ``harness__fetch``          — enqueue FETCH.
* ``harness__install``        — enqueue INSTALL.
* ``harness__sync``           — enqueue SYNC.
* ``harness__uninstall``      — enqueue UNINSTALL.

Every handler mirrors the logic in
:mod:`primer.api.routers.harness` without the HTTP layer.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pydantic import SecretStr

from primer.model.chat import Tool, ToolCallResult, ToolExample
from primer.toolset._describe import make_tool
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.harness import Harness, HarnessOperation, HarnessRendering, HarnessStatus
from primer.model.storage import (
    FieldRef,
    OffsetPage,
    Op,
    Predicate,
    Value,
)
from primer.toolset._helpers import err as _err, ok_json as _ok
from primer.toolset.internal import InternalToolsetProvider, ToolHandler


if TYPE_CHECKING:
    from primer.bus.in_memory import InMemoryEventBus
    from primer.int.storage_provider import StorageProvider


logger = logging.getLogger(__name__)

HARNESS_TOOLSET_ID = "harness"


# ---------------------------------------------------------------------------
# Helpers (``_ok`` / ``_err`` are the shared toolset result builders, imported
# above as aliases over :mod:`primer.toolset._helpers`).
# ---------------------------------------------------------------------------


def _harness_dict(harness: Harness) -> dict:
    """Serialize harness to JSON-safe dict with SecretStr redacted."""
    return harness.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Tool descriptors
# ---------------------------------------------------------------------------

TOOL_LIST = make_tool(
    id="harness__list",
    toolset_id=HARNESS_TOOLSET_ID,
    purpose=(
        "List harnesses, optionally filtered, as a paginated response "
        "(``items``, ``total``, ``offset``, ``length``)."
    ),
    when=(
        "Use when you need to enumerate or filter harnesses; not for "
        "fetching one known id (use ``harness__get``)."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "slug": {"type": "string", "description": "Filter by exact slug."},
            "status": {
                "type": "string",
                "enum": ["draft", "ready", "installed", "outdated", "error"],
                "description": "Filter by harness status.",
            },
            "offset": {"type": "integer", "minimum": 0, "default": 0},
            "length": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
        },
    },
    examples=[
        ToolExample(args={}, returns="first page of harnesses"),
        ToolExample(args={"status": "installed", "length": 20}, returns="installed only"),
    ],
)

TOOL_GET = make_tool(
    id="harness__get",
    toolset_id=HARNESS_TOOLSET_ID,
    purpose="Get a single harness by id, returning the full row.",
    when=(
        "Use when you have a known harness id; not for searching or "
        "filtering (use ``harness__list``). Returns ``is_error=true`` "
        "``type=not-found`` when the id is unknown."
    ),
    args_schema={
        "type": "object",
        "required": ["id"],
        "properties": {
            "id": {"type": "string", "description": "The harness id (e.g. hns_abc123)."},
        },
    },
    examples=[
        ToolExample(args={"id": "hns-1"}, returns="the harness row or not-found"),
    ],
)

TOOL_REGISTER = make_tool(
    id="harness__register",
    toolset_id=HARNESS_TOOLSET_ID,
    purpose="Register a new git-backed harness, creating it in status DRAFT.",
    when=(
        "Use when onboarding a new harness from a git repo; the row starts "
        "as DRAFT, so call ``harness__fetch`` next to load its schema. Slug "
        "must be unique (returns ``type=conflict`` otherwise)."
    ),
    args_schema={
        "type": "object",
        "required": ["name", "slug", "git_url"],
        "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": 200},
            "slug": {
                "type": "string",
                "pattern": "^[a-z][a-z0-9-]{1,63}$",
                "description": "Unique kebab-case identifier.",
            },
            "git_url": {"type": "string", "minLength": 1},
            "ref": {"type": "string", "minLength": 1, "description": "Git ref. Defaults to 'main'."},
            "subpath": {"type": "string", "description": "Subdirectory within the repo."},
            "git_token": {"type": "string", "description": "Personal access token (stored encrypted)."},
            "description": {"type": "string", "maxLength": 2000},
        },
    },
    examples=[
        ToolExample(
            args={"name": "My Harness", "slug": "my-harness", "git_url": "https://github.com/acme/h"},
            returns="DRAFT harness",
            note="call harness__fetch next",
        ),
    ],
)

TOOL_UPDATE = make_tool(
    id="harness__update",
    toolset_id=HARNESS_TOOLSET_ID,
    purpose="Update mutable metadata on a harness (name, description, ref, subpath, git_token).",
    when=(
        "Use when changing a harness's metadata; not for changing override "
        "values (use ``harness__update_overrides``). Changing ``ref`` or "
        "``subpath`` marks overrides dirty."
    ),
    args_schema={
        "type": "object",
        "required": ["id"],
        "properties": {
            "id": {"type": "string"},
            "name": {"type": "string", "minLength": 1, "maxLength": 200},
            "description": {"type": "string", "maxLength": 2000},
            "ref": {"type": "string", "minLength": 1},
            "subpath": {"type": "string"},
            "git_token": {"type": "string"},
        },
    },
    examples=[
        ToolExample(args={"id": "hns-1", "description": "new text"}, returns="updated row"),
    ],
)

TOOL_UPDATE_OVERRIDES = make_tool(
    id="harness__update_overrides",
    toolset_id=HARNESS_TOOLSET_ID,
    purpose="Validate overrides against the cached schema and store them.",
    when=(
        "Use when setting a harness's override values; not for metadata "
        "(use ``harness__update``). Returns ``type=overrides-invalid`` if "
        "validation fails, or ``type=overrides-schema-missing`` if no schema "
        "is cached yet (run ``harness__fetch`` first)."
    ),
    args_schema={
        "type": "object",
        "required": ["id", "overrides"],
        "properties": {
            "id": {"type": "string"},
            "overrides": {"type": "object", "description": "Override values dict."},
        },
    },
    examples=[
        ToolExample(
            args={"id": "hns-1", "overrides": {"some_key": "value"}},
            returns="stored overrides",
            note="validated against cached schema",
        ),
    ],
)

TOOL_FETCH = make_tool(
    id="harness__fetch",
    toolset_id=HARNESS_TOOLSET_ID,
    purpose="Enqueue a FETCH operation to load the harness bundle and overrides schema.",
    when=(
        "Use when you need to load or refresh a harness's bundle/schema "
        "(e.g. right after registering); not to apply it (use "
        "``harness__install``). Returns ``type=conflict`` if an operation is "
        "already pending."
    ),
    args_schema={
        "type": "object",
        "required": ["id"],
        "properties": {
            "id": {"type": "string"},
        },
    },
    examples=[
        ToolExample(args={"id": "hns-1"}, returns="enqueued FETCH"),
    ],
)

TOOL_INSTALL = make_tool(
    id="harness__install",
    toolset_id=HARNESS_TOOLSET_ID,
    purpose="Enqueue an INSTALL operation to apply the harness.",
    when=(
        "Use when activating a fetched harness; requires status in "
        "[draft, ready, outdated] and an overrides schema cached "
        "(run ``harness__fetch`` first). Returns ``type=conflict`` or "
        "``type=overrides-schema-missing`` when preconditions are unmet."
    ),
    args_schema={
        "type": "object",
        "required": ["id"],
        "properties": {
            "id": {"type": "string"},
        },
    },
    examples=[
        ToolExample(
            args={"id": "hns-1"},
            returns="enqueued INSTALL",
            note="requires status draft/ready/outdated + overrides cached",
        ),
    ],
)

TOOL_SYNC = make_tool(
    id="harness__sync",
    toolset_id=HARNESS_TOOLSET_ID,
    purpose="Enqueue a SYNC operation to reconcile an installed harness.",
    when=(
        "Use when re-applying an already-installed harness; requires status "
        "in [installed, outdated] and a fetched bundle. Returns "
        "``type=conflict`` or ``type=fetch-required`` when preconditions are "
        "unmet; not for first activation (use ``harness__install``)."
    ),
    args_schema={
        "type": "object",
        "required": ["id"],
        "properties": {
            "id": {"type": "string"},
        },
    },
    examples=[
        ToolExample(
            args={"id": "hns-1"},
            returns="enqueued SYNC",
            note="requires status installed/outdated",
        ),
    ],
)

TOOL_UNINSTALL = make_tool(
    id="harness__uninstall",
    toolset_id=HARNESS_TOOLSET_ID,
    purpose="Enqueue an UNINSTALL operation to remove an installed harness.",
    when=(
        "Use when tearing down a harness; not to pause/refresh it (use "
        "``harness__sync``). Returns ``type=conflict`` if an operation is "
        "already pending."
    ),
    args_schema={
        "type": "object",
        "required": ["id"],
        "properties": {
            "id": {"type": "string"},
        },
    },
    examples=[
        ToolExample(args={"id": "hns-1"}, returns="enqueued UNINSTALL"),
    ],
)


# ---------------------------------------------------------------------------
# Handler factories
# ---------------------------------------------------------------------------


def _make_list_handler(storage_provider: "StorageProvider") -> ToolHandler:
    async def _handler(arguments: dict[str, Any]) -> ToolCallResult:
        storage = storage_provider.get_storage(Harness)
        slug: str | None = arguments.get("slug")
        status_raw: str | None = arguments.get("status")
        offset: int = int(arguments.get("offset", 0))
        length: int = int(arguments.get("length", 50))
        page = OffsetPage(offset=offset, length=length)

        predicates: list[Predicate] = []
        if slug is not None:
            predicates.append(
                Predicate(left=FieldRef(name="slug"), op=Op.EQ, right=Value(value=slug))
            )
        if status_raw is not None:
            predicates.append(
                Predicate(
                    left=FieldRef(name="status"),
                    op=Op.EQ,
                    right=Value(value=status_raw),
                )
            )

        if not predicates:
            result = await storage.list(page)
        else:
            pred = predicates[0]
            for p in predicates[1:]:
                pred = Predicate(left=pred, op=Op.AND, right=p)
            result = await storage.find(pred, page)

        items = [_harness_dict(h) for h in result.items]
        return _ok({"items": items, "total": result.total, "offset": result.offset, "length": result.length})

    return _handler


def _make_get_handler(storage_provider: "StorageProvider") -> ToolHandler:
    async def _handler(arguments: dict[str, Any]) -> ToolCallResult:
        harness_id: str = arguments["id"]
        storage = storage_provider.get_storage(Harness)
        harness = await storage.get(harness_id)
        if harness is None:
            return _err(f"Harness {harness_id!r} does not exist", error_type="not-found")
        return _ok(_harness_dict(harness))

    return _handler


def _make_register_handler(storage_provider: "StorageProvider") -> ToolHandler:
    async def _handler(arguments: dict[str, Any]) -> ToolCallResult:
        storage = storage_provider.get_storage(Harness)

        name: str = arguments["name"]
        slug: str = arguments["slug"]
        git_url: str = arguments["git_url"]
        ref: str = arguments.get("ref") or "main"
        subpath: str | None = arguments.get("subpath")
        git_token_raw: str | None = arguments.get("git_token")
        description: str | None = arguments.get("description")

        # Enforce slug uniqueness
        slug_pred = Predicate(
            left=FieldRef(name="slug"),
            op=Op.EQ,
            right=Value(value=slug),
        )
        existing_page = await storage.find(slug_pred, OffsetPage(offset=0, length=1))
        items = list(getattr(existing_page, "items", []))
        if items:
            return _err(
                f"A harness with slug {slug!r} already exists",
                error_type="conflict",
            )

        harness_id = f"hns_{uuid4().hex[:12]}"
        harness = Harness(
            id=harness_id,
            slug=slug,
            name=name,
            description=description,
            git_url=git_url,
            git_token=SecretStr(git_token_raw) if git_token_raw else None,
            ref=ref,
            subpath=subpath,
            status=HarnessStatus.DRAFT,
            created_at=datetime.now(timezone.utc),
        )
        created = await storage.create(harness)
        return _ok(_harness_dict(created))

    return _handler


def _make_update_handler(storage_provider: "StorageProvider") -> ToolHandler:
    async def _handler(arguments: dict[str, Any]) -> ToolCallResult:
        harness_id: str = arguments["id"]
        storage = storage_provider.get_storage(Harness)
        harness = await storage.get(harness_id)
        if harness is None:
            return _err(f"Harness {harness_id!r} does not exist", error_type="not-found")

        overrides_dirty = harness.overrides_dirty

        name: str | None = arguments.get("name")
        description: str | None = arguments.get("description")
        ref: str | None = arguments.get("ref")
        subpath: str | None = arguments.get("subpath")
        git_token_raw: str | None = arguments.get("git_token")

        if name is not None:
            harness.name = name
        if description is not None:
            harness.description = description
        if ref is not None and ref != harness.ref:
            harness.ref = ref
            overrides_dirty = True
        if subpath is not None and subpath != harness.subpath:
            harness.subpath = subpath
            overrides_dirty = True
        if git_token_raw is not None:
            harness.git_token = SecretStr(git_token_raw)

        harness.overrides_dirty = overrides_dirty
        updated = await storage.update(harness)
        return _ok(_harness_dict(updated))

    return _handler


def _make_update_overrides_handler(storage_provider: "StorageProvider") -> ToolHandler:
    async def _handler(arguments: dict[str, Any]) -> ToolCallResult:
        harness_id: str = arguments["id"]
        overrides_body: dict[str, Any] = arguments.get("overrides", {})

        storage = storage_provider.get_storage(Harness)
        harness = await storage.get(harness_id)
        if harness is None:
            return _err(f"Harness {harness_id!r} does not exist", error_type="not-found")

        if harness.overrides_schema is None:
            return _err(
                "No overrides schema cached for this harness",
                error_type="overrides-schema-missing",
            )

        # Validate against the cached schema
        try:
            import jsonschema

            jsonschema.validate(instance=overrides_body, schema=harness.overrides_schema)
        except Exception as exc:
            errors = []
            if hasattr(exc, "message"):
                errors.append(str(exc.message))
            else:
                errors.append(str(exc))
            return _err(
                "Overrides validation failed: " + "; ".join(errors),
                error_type="overrides-invalid",
            )

        from primer.harness.hashes import hash_overrides

        harness.overrides = overrides_body
        harness.overrides_hash = hash_overrides(overrides_body)

        # Recompute overrides_dirty against the HarnessRendering snapshot
        rendering_storage = storage_provider.get_storage(HarnessRendering)
        rendering = await rendering_storage.get(harness_id)
        if rendering is not None:
            harness.overrides_dirty = harness.overrides_hash != rendering.overrides_hash
        else:
            harness.overrides_dirty = False

        updated = await storage.update(harness)
        return _ok(_harness_dict(updated))

    return _handler


def _make_enqueue_handler(
    storage_provider: "StorageProvider",
    event_bus: Any,
    operation: HarnessOperation,
) -> ToolHandler:
    """Build a handler for FETCH or UNINSTALL (simple enqueue with 409 guard)."""

    async def _handler(arguments: dict[str, Any]) -> ToolCallResult:
        harness_id: str = arguments["id"]
        storage = storage_provider.get_storage(Harness)
        harness = await storage.get(harness_id)
        if harness is None:
            return _err(f"Harness {harness_id!r} does not exist", error_type="not-found")

        if harness.pending_operation is not None:
            return _err(
                f"Harness {harness_id!r} already has a pending operation: "
                f"{harness.pending_operation.value!r}",
                error_type="conflict",
            )

        harness.pending_operation = operation
        updated = await storage.update(harness)
        if event_bus is not None:
            await event_bus.publish("harness-claimable", {"harness_id": harness_id})
        return _ok(_harness_dict(updated))

    return _handler


def _make_install_handler(storage_provider: "StorageProvider", event_bus: Any) -> ToolHandler:
    _INSTALL_ALLOWED = {HarnessStatus.DRAFT, HarnessStatus.READY, HarnessStatus.OUTDATED}

    async def _handler(arguments: dict[str, Any]) -> ToolCallResult:
        harness_id: str = arguments["id"]
        storage = storage_provider.get_storage(Harness)
        harness = await storage.get(harness_id)
        if harness is None:
            return _err(f"Harness {harness_id!r} does not exist", error_type="not-found")

        if harness.pending_operation is not None:
            return _err(
                f"Harness {harness_id!r} already has a pending operation: "
                f"{harness.pending_operation.value!r}",
                error_type="conflict",
            )

        if harness.status not in _INSTALL_ALLOWED:
            return _err(
                f"Harness {harness_id!r} status is {harness.status.value!r}; "
                f"install requires one of {[s.value for s in _INSTALL_ALLOWED]}",
                error_type="conflict",
            )

        if harness.overrides_schema is None:
            return _err(
                "No overrides schema cached; run harness__fetch first",
                error_type="overrides-schema-missing",
            )

        # Always validate the current overrides against the schema — even
        # for empty {} (the schema may declare required fields, in which
        # case empty input is invalid input, not a no-op).
        try:
            import jsonschema

            jsonschema.validate(
                instance=harness.overrides, schema=harness.overrides_schema,
            )
        except Exception as exc:
            errors = []
            if hasattr(exc, "message"):
                errors.append(str(exc.message))
            else:
                errors.append(str(exc))
            return _err(
                "Current overrides are invalid: " + "; ".join(errors),
                error_type="overrides-invalid",
            )

        harness.pending_operation = HarnessOperation.INSTALL
        updated = await storage.update(harness)
        if event_bus is not None:
            await event_bus.publish("harness-claimable", {"harness_id": harness_id})
        return _ok(_harness_dict(updated))

    return _handler


def _make_sync_handler(storage_provider: "StorageProvider", event_bus: Any) -> ToolHandler:
    _SYNC_ALLOWED = {HarnessStatus.INSTALLED, HarnessStatus.OUTDATED}

    async def _handler(arguments: dict[str, Any]) -> ToolCallResult:
        harness_id: str = arguments["id"]
        storage = storage_provider.get_storage(Harness)
        harness = await storage.get(harness_id)
        if harness is None:
            return _err(f"Harness {harness_id!r} does not exist", error_type="not-found")

        if harness.pending_operation is not None:
            return _err(
                f"Harness {harness_id!r} already has a pending operation: "
                f"{harness.pending_operation.value!r}",
                error_type="conflict",
            )

        if harness.status not in _SYNC_ALLOWED:
            return _err(
                f"Harness {harness_id!r} status is {harness.status.value!r}; "
                f"sync requires one of {[s.value for s in _SYNC_ALLOWED]}",
                error_type="conflict",
            )

        if harness.available_bundle_hash is None:
            return _err(
                "No bundle fetched yet; run harness__fetch first",
                error_type="fetch-required",
            )

        harness.pending_operation = HarnessOperation.SYNC
        updated = await storage.update(harness)
        if event_bus is not None:
            await event_bus.publish("harness-claimable", {"harness_id": harness_id})
        return _ok(_harness_dict(updated))

    return _handler


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


def build_harness_toolset_provider(
    *,
    storage_provider: "StorageProvider",
    event_bus: Any = None,
    toolset_id: str = HARNESS_TOOLSET_ID,
) -> InternalToolsetProvider:
    """Construct the ``harness`` internal toolset."""
    registry: dict[str, tuple[Tool, ToolHandler]] = {
        "harness__list": (TOOL_LIST, _make_list_handler(storage_provider)),
        "harness__get": (TOOL_GET, _make_get_handler(storage_provider)),
        "harness__register": (TOOL_REGISTER, _make_register_handler(storage_provider)),
        "harness__update": (TOOL_UPDATE, _make_update_handler(storage_provider)),
        "harness__update_overrides": (
            TOOL_UPDATE_OVERRIDES,
            _make_update_overrides_handler(storage_provider),
        ),
        "harness__fetch": (
            TOOL_FETCH,
            _make_enqueue_handler(storage_provider, event_bus, HarnessOperation.FETCH),
        ),
        "harness__install": (TOOL_INSTALL, _make_install_handler(storage_provider, event_bus)),
        "harness__sync": (TOOL_SYNC, _make_sync_handler(storage_provider, event_bus)),
        "harness__uninstall": (
            TOOL_UNINSTALL,
            _make_enqueue_handler(storage_provider, event_bus, HarnessOperation.UNINSTALL),
        ),
    }
    logger.info(
        "harness toolset assembled with %d tools (id=%s)",
        len(registry),
        toolset_id,
    )
    return InternalToolsetProvider(toolset_id=toolset_id, registry=registry)


__all__ = ["HARNESS_TOOLSET_ID", "build_harness_toolset_provider"]
