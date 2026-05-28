"""Built-in ``system`` toolset — exposes the full REST surface as agent tools.

The system toolset is **immutable** (its provider instance is constructed
once at app startup and lives for the process lifetime) and **reserved**
(its toolset id ``system`` short-circuits the normal ``Toolset`` row
lookup in :class:`primer.api.registries.ProviderRegistry`). It dogfoods
the entire primer REST API to agents so they can self-administer the
configuration that drives them.

Tool catalog
------------

Per-entity CRUD set (10 entities × 6 tools = 60 tools) for:
    llm_provider, embedding_provider, cross_encoder_provider, toolset,
    agent, graph, collection, document, agent_thread, graph_thread,
    semantic_search_provider

Plus entity-specific operations:
* ``fetch_llm_provider_models``, ``fetch_embedding_provider_models``,
  ``fetch_cross_encoder_provider_models`` — live model lists.
* ``list_toolset_tools`` — enumerate the tools a toolset exposes.
* ``call_tool`` — meta-dispatch: invoke any tool from any toolset.
* Agent threads CRUD — ``list/get/create/update/delete_agent_thread``.
* Graph threads CRUD — ``list/get/create/update/delete_graph_thread``.
* Collection extras — ``list_collection_documents``,
  ``find_collection_documents_by_meta``, ``search_collection``,
  ``refresh_collection``.
* Document extras — ``get_document_content``, ``put_document``.

Total: ~75 tools. ``search_collection`` and ``refresh_collection`` are
stubbed with ``is_error=True`` until the SearchService pipeline lands.

Cascade invalidation
--------------------

Mutations on rows backed by a cached adapter (LLMProvider,
EmbeddingProvider, CrossEncoderProvider, Toolset, VectorStoreConfig)
invoke the matching ``invalidate_*`` on the registry so the next
read/call rebuilds the adapter from the new row.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, ValidationError

from primer.model.agent import Agent
from primer.model.chat import Tool, ToolCallResult
from primer.model.collection import Collection, Document
from primer.model.common import Identifiable
from primer.model.except_ import (
    ConflictError,
    MatrixError,
    NotFoundError,
)
from primer.model.graph import Graph, GraphThread
from primer.model.provider import (
    CrossEncoderProvider,
    EmbeddingProvider,
    LLMProvider,
    SemanticSearchProvider,
    Toolset,
)
from primer.model.storage import (
    CursorPage,
    OffsetPage,
    OrderBy,
    Predicate,
)
from primer.model.thread import Thread
from primer.model.channel import (
    Channel,
    ChannelProvider,
    WorkspaceChannelAssociation,
)
from primer.model.tool_approval import ToolApprovalPolicy
from primer.toolset.internal import InternalToolsetProvider, ToolHandler


if TYPE_CHECKING:
    from primer.api.registries import ProviderRegistry
    from primer.api.registries.semantic_search_registry import SemanticSearchRegistry
    from primer.int.storage_provider import StorageProvider


logger = logging.getLogger(__name__)


SYSTEM_TOOLSET_ID = "system"


# ===========================================================================
# Helpers — JSON encoding + uniform error wrapping
# ===========================================================================


def _to_json(payload: Any) -> str:
    if isinstance(payload, BaseModel):
        return payload.model_dump_json()
    if isinstance(payload, list):
        return json.dumps(
            [
                p.model_dump(mode="json") if isinstance(p, BaseModel) else p
                for p in payload
            ],
            default=str,
        )
    return json.dumps(payload, default=str)


def _ok(payload: Any) -> ToolCallResult:
    return ToolCallResult(output=_to_json(payload), is_error=False)


def _err(message: str, *, error_type: str = "tool-error") -> ToolCallResult:
    return ToolCallResult(
        output=json.dumps({"type": error_type, "message": message}),
        is_error=True,
    )


def _err_from_matrix(exc: MatrixError, *, error_type: str) -> ToolCallResult:
    return _err(getattr(exc, "message", str(exc)), error_type=error_type)


def _err_from_validation(exc: ValidationError) -> ToolCallResult:
    return _err(
        "argument validation failed: " + json.dumps(exc.errors(), default=str),
        error_type="validation-error",
    )


# ===========================================================================
# Argument models — shared shapes
# ===========================================================================


class _GetByIdArgs(BaseModel):
    """Look up an entity by its id."""

    id: str = Field(..., min_length=1, description="Entity id (case-sensitive).")


class _DeleteByIdArgs(BaseModel):
    """Delete an entity by its id."""

    id: str = Field(..., min_length=1, description="Entity id (case-sensitive).")


class _PaginationArgs(BaseModel):
    """Page selector — supply EITHER ``offset`` OR ``cursor``, never both."""

    limit: int = Field(
        default=20,
        ge=1,
        le=200,
        description="Maximum number of items returned (1-200, default 20).",
    )
    offset: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Offset-based: number of items to skip. Mutually exclusive "
            "with ``cursor``. If both are omitted, defaults to offset 0."
        ),
    )
    cursor: str | None = Field(
        default=None,
        description=(
            "Cursor-based: opaque cursor returned as ``next_cursor`` by "
            "a prior list call. Mutually exclusive with ``offset``."
        ),
    )
    order_by: list[str] | None = Field(
        default=None,
        description=(
            "Sort spec, e.g. ``['id:asc', 'name:desc']``. Each entry "
            "is ``field:direction`` where direction is ``asc`` or ``desc``. "
            "Direction defaults to ``asc`` if omitted."
        ),
    )


class _FindArgs(_PaginationArgs):
    """Predicate-based search arguments."""

    predicate: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Predicate tree (see :class:`primer.model.storage.Predicate`). "
            "Binary tree of comparison/logical ops. Each node is "
            "``{kind:'predicate', left:..., op:..., right:...}``; leaf "
            "field references are ``{kind:'field', name:'...'}`` and "
            "literal values are ``{kind:'value', value:...}``. "
            "Operators: =, !=, ~=, >, <, >=, <=, in, and, or. "
            "Pass ``null`` to find all rows (equivalent to list)."
        ),
    )


def _parse_page(args: _PaginationArgs) -> OffsetPage | CursorPage:
    if args.offset is not None and args.cursor is not None:
        raise ValueError("supply either ``offset`` or ``cursor``, not both")
    if args.cursor is not None:
        return CursorPage(cursor=args.cursor, length=args.limit)
    return OffsetPage(offset=args.offset or 0, length=args.limit)


def _parse_order_by(spec: list[str] | None) -> list[OrderBy] | None:
    if spec is None:
        return None
    parsed: list[OrderBy] = []
    for entry in spec:
        if ":" in entry:
            field, direction = entry.split(":", 1)
            direction = direction.strip().lower() or "asc"
            if direction not in ("asc", "desc"):
                raise ValueError(
                    f"invalid order_by direction {direction!r} in {entry!r}; "
                    "must be 'asc' or 'desc'"
                )
        else:
            field, direction = entry, "asc"
        parsed.append(OrderBy(field=field.strip(), direction=direction))  # type: ignore[arg-type]
    return parsed


# ===========================================================================
# Generic CRUD tool factory — produces 6 tools per entity
# ===========================================================================


_OnMutate = Callable[[str], Awaitable[None]] | None


def _crud_tools_for(
    *,
    entity_label: str,
    entity_label_plural: str,
    model_cls: type[Identifiable],
    storage_provider: "StorageProvider",
    on_create: _OnMutate = None,
    on_update: _OnMutate = None,
    on_delete: _OnMutate = None,
) -> dict[str, tuple[Tool, ToolHandler]]:
    """Build ``list/get/create/update/delete/find_<entity>`` tools.

    Schemas embed the entity model's full JSON schema for create/update
    so the LLM sees every required and optional field on the entity
    type rather than a generic dict.
    """
    storage = storage_provider.get_storage(model_cls)
    cls_name = model_cls.__name__
    tools: dict[str, tuple[Tool, ToolHandler]] = {}
    entity_schema = model_cls.model_json_schema()

    # ---- list ---------------------------------------------------------
    async def _list_handler(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _PaginationArgs.model_validate(arguments)
            page = _parse_page(args)
            order_by = _parse_order_by(args.order_by)
        except (ValidationError, ValueError) as exc:
            if isinstance(exc, ValidationError):
                return _err_from_validation(exc)
            return _err(str(exc), error_type="bad-request")
        try:
            response = await storage.list(page, order_by=order_by)
        except MatrixError as exc:
            return _err_from_matrix(exc, error_type="storage-error")
        return _ok(response)

    tools[f"list_{entity_label_plural}"] = (
        Tool(
            id=f"list_{entity_label_plural}",
            description=(
                f"List {cls_name} entities with pagination. Supply "
                "``offset`` (default 0) OR ``cursor`` (mutually exclusive). "
                "``limit`` defaults to 20, max 200. Optional ``order_by`` "
                "sort spec like ``['id:asc']``. Returns a page object "
                "with ``items`` (the entities), ``length`` (count this "
                "page), ``total`` (full set count, offset mode only), "
                "and ``next_cursor`` (cursor mode only). On invalid "
                "arguments returns ``is_error=true``."
            ),
            toolset_id=SYSTEM_TOOLSET_ID,
            args_schema=_PaginationArgs.model_json_schema(),
        ),
        _list_handler,
    )

    # ---- get ----------------------------------------------------------
    async def _get_handler(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _GetByIdArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        row = await storage.get(args.id)
        if row is None:
            return _err(
                f"{cls_name} {args.id!r} does not exist", error_type="not-found"
            )
        return _ok(row)

    tools[f"get_{entity_label}"] = (
        Tool(
            id=f"get_{entity_label}",
            description=(
                f"Fetch one {cls_name} by its ``id``. Returns the full "
                "entity object on success. Returns ``is_error=true`` "
                "with ``type=not-found`` if no row has that id."
            ),
            toolset_id=SYSTEM_TOOLSET_ID,
            args_schema=_GetByIdArgs.model_json_schema(),
        ),
        _get_handler,
    )

    # ---- create -------------------------------------------------------
    async def _create_handler(arguments: dict[str, Any]) -> ToolCallResult:
        body = arguments.get("entity")
        if body is None:
            return _err(
                "missing required argument 'entity'", error_type="bad-request"
            )
        try:
            entity = model_cls.model_validate(body)
        except ValidationError as exc:
            return _err_from_validation(exc)
        existing = await storage.get(entity.id)
        if existing is not None:
            return _err(
                f"{cls_name} with id {entity.id!r} already exists",
                error_type="conflict",
            )
        try:
            created = await storage.create(entity)
        except (ConflictError, MatrixError) as exc:
            return _err_from_matrix(exc, error_type="storage-error")
        if on_create is not None:
            await on_create(created.id)
        return _ok(created)

    tools[f"create_{entity_label}"] = (
        Tool(
            id=f"create_{entity_label}",
            description=(
                f"Create a new {cls_name}. Body shape is the full "
                f"{cls_name} schema (see ``entity`` parameter). The "
                "entity's ``id`` field is the wire-level identifier — "
                "must be unique across this entity type. Returns the "
                "created entity (echoes back the body). On duplicate id "
                "returns ``is_error=true`` with ``type=conflict``; on "
                "schema violation returns ``type=validation-error``."
            ),
            toolset_id=SYSTEM_TOOLSET_ID,
            args_schema={
                "type": "object",
                "properties": {"entity": entity_schema},
                "required": ["entity"],
            },
        ),
        _create_handler,
    )

    # ---- update -------------------------------------------------------
    async def _update_handler(arguments: dict[str, Any]) -> ToolCallResult:
        entity_id = arguments.get("id")
        body = arguments.get("entity")
        if not entity_id:
            return _err("missing required argument 'id'", error_type="bad-request")
        if body is None:
            return _err(
                "missing required argument 'entity'", error_type="bad-request"
            )
        try:
            entity = model_cls.model_validate(body)
        except ValidationError as exc:
            return _err_from_validation(exc)
        if entity.id != entity_id:
            return _err(
                f"path id {entity_id!r} does not match body id {entity.id!r}",
                error_type="conflict",
            )
        existing = await storage.get(entity_id)
        if existing is None:
            return _err(
                f"{cls_name} {entity_id!r} does not exist", error_type="not-found"
            )
        try:
            updated = await storage.update(entity)
        except MatrixError as exc:
            return _err_from_matrix(exc, error_type="storage-error")
        if on_update is not None:
            await on_update(updated.id)
        return _ok(updated)

    tools[f"update_{entity_label}"] = (
        Tool(
            id=f"update_{entity_label}",
            description=(
                f"Replace an existing {cls_name}. Pass ``id`` (the row "
                f"to update) and ``entity`` (the full new {cls_name} "
                "body — replaces, does not patch). The body's ``id`` "
                "must equal the path ``id``. Returns the updated entity. "
                "Mutating provider/toolset/vector-store rows cascades to "
                "invalidate the matching cached adapter. On unknown id "
                "returns ``type=not-found``; on id mismatch returns "
                "``type=conflict``."
            ),
            toolset_id=SYSTEM_TOOLSET_ID,
            args_schema={
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Id of the row to replace.",
                    },
                    "entity": entity_schema,
                },
                "required": ["id", "entity"],
            },
        ),
        _update_handler,
    )

    # ---- delete -------------------------------------------------------
    async def _delete_handler(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _DeleteByIdArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        existing = await storage.get(args.id)
        if existing is None:
            return _err(
                f"{cls_name} {args.id!r} does not exist", error_type="not-found"
            )
        try:
            await storage.delete(args.id)
        except MatrixError as exc:
            return _err_from_matrix(exc, error_type="storage-error")
        if on_delete is not None:
            await on_delete(args.id)
        return _ok({"deleted": True, "id": args.id})

    tools[f"delete_{entity_label}"] = (
        Tool(
            id=f"delete_{entity_label}",
            description=(
                f"Delete a {cls_name} by ``id``. Cascades to invalidate "
                "the cached adapter for provider/toolset/vector-store "
                "rows. Returns ``{'deleted': true, 'id': '...'}`` on "
                "success. Returns ``type=not-found`` if no row has that "
                "id."
            ),
            toolset_id=SYSTEM_TOOLSET_ID,
            args_schema=_DeleteByIdArgs.model_json_schema(),
        ),
        _delete_handler,
    )

    # ---- find ---------------------------------------------------------
    async def _find_handler(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _FindArgs.model_validate(arguments)
            page = _parse_page(args)
            order_by = _parse_order_by(args.order_by)
            predicate: Predicate | None = None
            if args.predicate is not None:
                predicate = Predicate.model_validate(args.predicate)
        except (ValidationError, ValueError) as exc:
            if isinstance(exc, ValidationError):
                return _err_from_validation(exc)
            return _err(str(exc), error_type="bad-request")
        try:
            response = await storage.find(predicate, page, order_by=order_by)
        except MatrixError as exc:
            return _err_from_matrix(exc, error_type="storage-error")
        return _ok(response)

    tools[f"find_{entity_label_plural}"] = (
        Tool(
            id=f"find_{entity_label_plural}",
            description=(
                f"Find {cls_name} entities matching a predicate tree. "
                "Same pagination + ordering as ``list``. Pass "
                "``predicate=null`` to match everything (equivalent to "
                "list). The predicate is a binary tree — see "
                "the ``predicate`` parameter description for the full "
                "operator and operand shape."
            ),
            toolset_id=SYSTEM_TOOLSET_ID,
            args_schema=_FindArgs.model_json_schema(),
        ),
        _find_handler,
    )

    return tools


# ===========================================================================
# Provider-specific extras: fetch_models
# ===========================================================================


class _ProviderIdArgs(BaseModel):
    """Reference to a provider row by id."""

    provider_id: str = Field(
        ..., min_length=1, description="Id of the provider row to query."
    )


def _fetch_models_tool(
    *,
    label: str,
    pretty: str,
    registry: "ProviderRegistry",
    fetch_method: str,
) -> tuple[str, tuple[Tool, ToolHandler]]:
    """Build a ``fetch_<label>_models`` tool."""

    async def _handler(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _ProviderIdArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        try:
            adapter = await getattr(registry, fetch_method)(args.provider_id)
            models = await adapter.list_models()
        except NotFoundError as exc:
            return _err_from_matrix(exc, error_type="not-found")
        except MatrixError as exc:
            return _err_from_matrix(exc, error_type="provider-error")
        return _ok({"models": list(models)})

    name = f"fetch_{label}_models"
    return name, (
        Tool(
            id=name,
            description=(
                f"Live model list reported by the {pretty} provider. "
                "Differs from the configured ``models`` field on the "
                "provider row, which is what the application is "
                "*permitted* to send. Use this to discover model "
                "identifiers the provider currently exposes."
            ),
            toolset_id=SYSTEM_TOOLSET_ID,
            args_schema=_ProviderIdArgs.model_json_schema(),
        ),
        _handler,
    )


# ===========================================================================
# Toolset extras: list_toolset_tools, call_tool
# ===========================================================================


class _ToolsetIdArgs(BaseModel):
    toolset_id: str = Field(
        ..., min_length=1, description="Id of the toolset to query."
    )
    principal: str | None = Field(
        default=None,
        description=(
            "Optional end-user identity passed through to the toolset. "
            "Required for OAuth-protected MCP toolsets if the cached "
            "token is bound to a specific user."
        ),
    )


class _CallToolArgs(BaseModel):
    toolset_id: str = Field(..., min_length=1, description="Toolset id.")
    tool_name: str = Field(..., min_length=1, description="Tool wire id.")
    arguments: dict[str, Any] = Field(
        default_factory=dict, description="Argument object for the call."
    )
    principal: str | None = Field(
        default=None,
        description=(
            "Optional end-user identity. Required for OAuth-protected "
            "MCP toolsets if per-user token caching is in effect."
        ),
    )


def _list_toolset_tools_tool(
    registry: "ProviderRegistry",
) -> tuple[str, tuple[Tool, ToolHandler]]:
    async def _handler(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _ToolsetIdArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        try:
            provider = await registry.get_toolset(args.toolset_id)
        except NotFoundError as exc:
            return _err_from_matrix(exc, error_type="not-found")
        except MatrixError as exc:
            return _err_from_matrix(exc, error_type="provider-error")
        tools_out: list[dict[str, Any]] = []
        async for tool in provider.list_tools(principal=args.principal):
            tools_out.append(tool.model_dump(mode="json"))
        return _ok({"tools": tools_out})

    return "list_toolset_tools", (
        Tool(
            id="list_toolset_tools",
            description=(
                "Enumerate tools currently exposed by a toolset. Calls "
                "the live provider so OAuth-protected MCP toolsets may "
                "raise an auth-required error (returned as "
                "``is_error=true`` ``type=auth-required``). Returns "
                "``{'tools': [Tool, ...]}`` — each Tool carries its "
                "id, description, schema, and originating toolset_id."
            ),
            toolset_id=SYSTEM_TOOLSET_ID,
            args_schema=_ToolsetIdArgs.model_json_schema(),
        ),
        _handler,
    )


def _call_tool_tool(
    registry: "ProviderRegistry",
) -> tuple[str, tuple[Tool, ToolHandler]]:
    async def _handler(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _CallToolArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        try:
            provider = await registry.get_toolset(args.toolset_id)
        except NotFoundError as exc:
            return _err_from_matrix(exc, error_type="not-found")
        except MatrixError as exc:
            return _err_from_matrix(exc, error_type="provider-error")
        try:
            result = await provider.call(
                tool_name=args.tool_name,
                arguments=args.arguments,
                principal=args.principal,
            )
        except MatrixError as exc:
            return _err_from_matrix(exc, error_type="tool-call-error")
        return ToolCallResult(
            output=result.output,
            is_error=result.is_error,
            extended=result.extended,
        )

    return "call_tool", (
        Tool(
            id="call_tool",
            description=(
                "Meta-dispatch: invoke any tool from any toolset by id. "
                "Useful when you have discovered a tool via "
                "``list_toolset_tools`` and want to execute it without "
                "going through the dedicated agent toolset wiring. The "
                "dispatched tool's own ``output`` and ``is_error`` are "
                "passed through unchanged so the model can act on them."
            ),
            toolset_id=SYSTEM_TOOLSET_ID,
            args_schema=_CallToolArgs.model_json_schema(),
        ),
        _handler,
    )


# ===========================================================================
# Collection extras: list_documents, find_by_meta, search, refresh
# ===========================================================================


class _CollectionDocumentsListArgs(_PaginationArgs):
    collection_id: str = Field(
        ..., min_length=1, description="Collection id."
    )


class _CollectionFindByMetaArgs(_PaginationArgs):
    collection_id: str = Field(
        ..., min_length=1, description="Collection id."
    )
    meta_filter: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Key/value pairs to match against ``Document.meta``. "
            "Equality only — for richer predicates use ``find_documents``."
        ),
    )


class _CollectionSearchArgs(BaseModel):
    collection_id: str = Field(
        ..., min_length=1, description="Collection id."
    )
    query: str = Field(..., min_length=1, description="Free-text query string.")
    top_k: int = Field(
        default=10, ge=1, le=100, description="Maximum number of hits to return."
    )


class _CollectionIdArgs(BaseModel):
    collection_id: str = Field(
        ..., min_length=1, description="Collection id."
    )


def _collection_extras(
    *,
    storage_provider: "StorageProvider",
) -> dict[str, tuple[Tool, ToolHandler]]:
    collections = storage_provider.get_storage(Collection)
    documents = storage_provider.get_storage(Document)

    out: dict[str, tuple[Tool, ToolHandler]] = {}

    # ---- list_collection_documents -----------------------------------
    async def _list_docs(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _CollectionDocumentsListArgs.model_validate(arguments)
            page = _parse_page(args)
            order_by = _parse_order_by(args.order_by)
        except (ValidationError, ValueError) as exc:
            if isinstance(exc, ValidationError):
                return _err_from_validation(exc)
            return _err(str(exc), error_type="bad-request")
        if await collections.get(args.collection_id) is None:
            return _err(
                f"Collection {args.collection_id!r} does not exist",
                error_type="not-found",
            )
        from primer.model.storage import FieldRef, Op, Value

        predicate = Predicate(
            left=FieldRef(name="collection_id"),
            op=Op.EQ,
            right=Value(value=args.collection_id),
        )
        response = await documents.find(predicate, page, order_by=order_by)
        return _ok(response)

    out["list_collection_documents"] = (
        Tool(
            id="list_collection_documents",
            description=(
                "List documents belonging to a collection. Server-side "
                "filter on ``Document.collection_id == collection_id``. "
                "Same pagination contract as the entity ``list`` tools. "
                "Returns ``type=not-found`` if the collection itself "
                "does not exist."
            ),
            toolset_id=SYSTEM_TOOLSET_ID,
            args_schema=_CollectionDocumentsListArgs.model_json_schema(),
        ),
        _list_docs,
    )

    # ---- find_collection_documents_by_meta ---------------------------
    async def _find_by_meta(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _CollectionFindByMetaArgs.model_validate(arguments)
            page = _parse_page(args)
            order_by = _parse_order_by(args.order_by)
        except (ValidationError, ValueError) as exc:
            if isinstance(exc, ValidationError):
                return _err_from_validation(exc)
            return _err(str(exc), error_type="bad-request")
        if await collections.get(args.collection_id) is None:
            return _err(
                f"Collection {args.collection_id!r} does not exist",
                error_type="not-found",
            )
        from primer.model.storage import FieldRef, Op, Value

        node: Predicate = Predicate(
            left=FieldRef(name="collection_id"),
            op=Op.EQ,
            right=Value(value=args.collection_id),
        )
        for key, value in args.meta_filter.items():
            equality = Predicate(
                left=FieldRef(name=f"meta.{key}"),
                op=Op.EQ,
                right=Value(value=value),
            )
            node = Predicate(left=node, op=Op.AND, right=equality)

        response = await documents.find(node, page, order_by=order_by)
        return _ok(response)

    out["find_collection_documents_by_meta"] = (
        Tool(
            id="find_collection_documents_by_meta",
            description=(
                "Find documents in a collection by metadata equality. "
                "``meta_filter`` is a flat dict of "
                "``{key: equality_value, ...}`` translated server-side "
                "to ``meta.key == value`` predicates joined with AND. "
                "For richer predicates (range, OR, IN) use the generic "
                "``find_documents`` tool with a hand-built Predicate."
            ),
            toolset_id=SYSTEM_TOOLSET_ID,
            args_schema=_CollectionFindByMetaArgs.model_json_schema(),
        ),
        _find_by_meta,
    )

    # ---- search_collection (deferred — stubbed) -----------------------
    async def _search(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            _CollectionSearchArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        return _err(
            "semantic search requires the SearchService pipeline "
            "(embedder + vector store + optional cross-encoder) which "
            "is not yet wired in the API layer; use "
            "``find_collection_documents_by_meta`` for metadata "
            "filtering or ``find_documents`` for predicate search "
            "until then.",
            error_type="not-implemented",
        )

    out["search_collection"] = (
        Tool(
            id="search_collection",
            description=(
                "Semantic / hybrid search over a collection. STUB: "
                "returns ``is_error=true`` ``type=not-implemented`` "
                "until the SearchService pipeline lands. Use "
                "``find_collection_documents_by_meta`` for metadata "
                "filtering in the meantime."
            ),
            toolset_id=SYSTEM_TOOLSET_ID,
            args_schema=_CollectionSearchArgs.model_json_schema(),
        ),
        _search,
    )

    # ---- refresh_collection (deferred — stubbed) ----------------------
    async def _refresh(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _CollectionIdArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        if await collections.get(args.collection_id) is None:
            return _err(
                f"Collection {args.collection_id!r} does not exist",
                error_type="not-found",
            )
        return _err(
            "refresh re-vectorises every document in the collection "
            "and requires the SearchService ingestion pipeline; not "
            "yet implemented at the API layer.",
            error_type="not-implemented",
        )

    out["refresh_collection"] = (
        Tool(
            id="refresh_collection",
            description=(
                "Re-embed every document in the collection (e.g. after "
                "swapping the embedder). STUB: returns "
                "``is_error=true`` ``type=not-implemented`` until the "
                "ingestion pipeline lands."
            ),
            toolset_id=SYSTEM_TOOLSET_ID,
            args_schema=_CollectionIdArgs.model_json_schema(),
        ),
        _refresh,
    )

    return out


# ===========================================================================
# Document extras: get_document_content, put_document
# ===========================================================================


class _DocumentIdArgs(BaseModel):
    document_id: str = Field(..., min_length=1, description="Document id.")


class _PutDocumentArgs(BaseModel):
    """Ingest a document with content into a collection.

    Until the docling/embedding pipeline lands, ``content`` is stored
    verbatim under ``Document.meta['content']`` so it can be retrieved
    by ``get_document_content``. Once ingestion lands, content will
    flow through chunking + embedding before vector storage.
    """

    id: str = Field(
        ..., min_length=1, description="Document id (unique within the collection)."
    )
    collection_id: str = Field(
        ..., min_length=1, description="Parent collection id."
    )
    name: str = Field(
        ..., min_length=1, description="Human-readable document name."
    )
    content: str = Field(
        ...,
        min_length=1,
        description=(
            "Raw text content of the document. Stored under "
            "``meta['content']`` until the chunking pipeline lands."
        ),
    )
    meta: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Application-defined metadata. Merged with the synthesised "
            "``content`` key (your value wins on conflict)."
        ),
    )


def _document_extras(
    *,
    storage_provider: "StorageProvider",
) -> dict[str, tuple[Tool, ToolHandler]]:
    documents = storage_provider.get_storage(Document)
    out: dict[str, tuple[Tool, ToolHandler]] = {}

    async def _get_content(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _DocumentIdArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        doc = await documents.get(args.document_id)
        if doc is None:
            return _err(
                f"Document {args.document_id!r} does not exist",
                error_type="not-found",
            )
        content = (doc.meta or {}).get("content", "")
        return _ok(
            {
                "id": doc.id,
                "collection_id": doc.collection_id,
                "name": doc.name,
                "content": content,
            }
        )

    out["get_document_content"] = (
        Tool(
            id="get_document_content",
            description=(
                "Fetch a document's text content. Looks up "
                "``Document.meta['content']`` (where ``put_document`` "
                "stores the raw text). Returns ``{id, collection_id, "
                "name, content}``. Empty string content is normal for "
                "documents that were created without going through "
                "``put_document``."
            ),
            toolset_id=SYSTEM_TOOLSET_ID,
            args_schema=_DocumentIdArgs.model_json_schema(),
        ),
        _get_content,
    )

    async def _put_document(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _PutDocumentArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        merged_meta = {"content": args.content, **args.meta}
        doc = Document(
            id=args.id,
            collection_id=args.collection_id,
            name=args.name,
            meta=merged_meta,
        )
        existing = await documents.get(args.id)
        try:
            if existing is None:
                stored = await documents.create(doc)
            else:
                stored = await documents.update(doc)
        except MatrixError as exc:
            return _err_from_matrix(exc, error_type="storage-error")
        return _ok(stored)

    out["put_document"] = (
        Tool(
            id="put_document",
            description=(
                "Ingest a document into a collection (upsert). "
                "Synthesises a ``Document`` row, stores ``content`` "
                "under ``meta['content']``, and creates the row if new "
                "or updates it if the id already exists. Until the "
                "chunking + embedding pipeline lands, this does NOT "
                "vectorise the document — search will not see it. Use "
                "``create_document`` / ``update_document`` for the raw "
                "row-level CRUD without content semantics."
            ),
            toolset_id=SYSTEM_TOOLSET_ID,
            args_schema=_PutDocumentArgs.model_json_schema(),
        ),
        _put_document,
    )

    return out


# ===========================================================================
# Build the toolset
# ===========================================================================


def build_system_toolset(
    *,
    storage_provider: "StorageProvider",
    provider_registry: "ProviderRegistry",
    semantic_search_registry: "SemanticSearchRegistry | None" = None,
    toolset_id: str = SYSTEM_TOOLSET_ID,
) -> InternalToolsetProvider:
    """Construct the immutable ``_system`` toolset.

    Wires every CRUD set, entity-specific extras, and meta tools into
    a single :class:`InternalToolsetProvider`. Mutation cascades are
    threaded into the provider/vector-store registries so the system
    toolset stays consistent with the REST routers.
    """
    registry: dict[str, tuple[Tool, ToolHandler]] = {}

    # ---- Cascade-invalidation hooks -----------------------------------
    async def _inv_llm(eid: str) -> None:
        await provider_registry.invalidate_llm(eid)

    async def _inv_emb(eid: str) -> None:
        await provider_registry.invalidate_embedder(eid)

    async def _inv_ce(eid: str) -> None:
        await provider_registry.invalidate_cross_encoder(eid)

    async def _inv_ts(eid: str) -> None:
        await provider_registry.invalidate_toolset(eid)

    async def _inv_ssp(eid: str) -> None:
        if semantic_search_registry is not None:
            await semantic_search_registry.invalidate(eid)

    # ---- CRUD sets ----------------------------------------------------
    # Note: VectorStoreConfig was removed from this set when vector
    # store configuration moved into AppConfig (it is no longer a
    # storage row).
    crud_specs = [
        ("llm_provider", "llm_providers", LLMProvider, None, _inv_llm, _inv_llm),
        ("embedding_provider", "embedding_providers", EmbeddingProvider, None, _inv_emb, _inv_emb),
        ("cross_encoder_provider", "cross_encoder_providers", CrossEncoderProvider, None, _inv_ce, _inv_ce),
        ("toolset", "toolsets", Toolset, None, _inv_ts, _inv_ts),
        ("agent", "agents", Agent, None, None, None),
        ("graph", "graphs", Graph, None, None, None),
        ("collection", "collections", Collection, None, None, None),
        ("document", "documents", Document, None, None, None),
        ("agent_thread", "agent_threads", Thread, None, None, None),
        ("graph_thread", "graph_threads", GraphThread, None, None, None),
        ("semantic_search_provider", "semantic_search_providers", SemanticSearchProvider, None, _inv_ssp, _inv_ssp),
        ("tool_approval_policy", "tool_approval_policies", ToolApprovalPolicy, None, None, None),
        ("channel_provider", "channel_providers", ChannelProvider, None, None, None),
        ("channel", "channels", Channel, None, None, None),
        ("workspace_channel_association", "workspace_channel_associations", WorkspaceChannelAssociation, None, None, None),
    ]
    for label, plural, cls, on_c, on_u, on_d in crud_specs:
        registry.update(
            _crud_tools_for(
                entity_label=label,
                entity_label_plural=plural,
                model_cls=cls,
                storage_provider=storage_provider,
                on_create=on_c,
                on_update=on_u,
                on_delete=on_d,
            )
        )

    # ---- SemanticSearchProvider explicit invalidation tool -----------
    class _InvalidateSSPArgs(BaseModel):
        """Force-expire the cached VectorStoreProvider for one SSP row."""

        id: str = Field(
            ...,
            min_length=1,
            description=(
                "Id of the SemanticSearchProvider row whose cached "
                "VectorStoreProvider instance should be evicted. The "
                "next call that needs the backend will re-resolve the "
                "row from storage and reconstruct the adapter."
            ),
        )

    async def _invalidate_ssp_handler(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _InvalidateSSPArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        if semantic_search_registry is not None:
            await semantic_search_registry.invalidate(args.id)
        return _ok({"invalidated": True, "id": args.id})

    registry["invalidate_semantic_search_provider"] = (
        Tool(
            id="invalidate_semantic_search_provider",
            description=(
                "Expire the cached VectorStoreProvider adapter for a "
                "SemanticSearchProvider row. Call this after updating "
                "the provider row so the next search request rebuilds "
                "the adapter from the new config. Safe to call even if "
                "no cached instance exists (no-op). Returns "
                "``{'invalidated': true, 'id': '...'}``."
            ),
            toolset_id=SYSTEM_TOOLSET_ID,
            args_schema=_InvalidateSSPArgs.model_json_schema(),
        ),
        _invalidate_ssp_handler,
    )

    # ---- Provider-specific fetch_models ------------------------------
    for label, pretty, method in (
        ("llm_provider", "LLM", "get_llm"),
        ("embedding_provider", "embedding", "get_embedder"),
        ("cross_encoder_provider", "cross-encoder", "get_cross_encoder"),
    ):
        name, entry = _fetch_models_tool(
            label=label, pretty=pretty, registry=provider_registry, fetch_method=method
        )
        registry[name] = entry

    # ---- Toolset extras ---------------------------------------------
    for builder in (_list_toolset_tools_tool, _call_tool_tool):
        name, entry = builder(provider_registry)
        registry[name] = entry

    # ---- Collection / Document extras --------------------------------
    registry.update(_collection_extras(storage_provider=storage_provider))
    registry.update(_document_extras(storage_provider=storage_provider))

    logger.info(
        "system toolset assembled with %d tools (id=%s)",
        len(registry),
        toolset_id,
    )

    return InternalToolsetProvider(toolset_id=toolset_id, registry=registry)


__all__ = ["SYSTEM_TOOLSET_ID", "build_system_toolset"]
