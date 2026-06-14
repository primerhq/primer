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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, ValidationError, create_model

from primer.agent.invoke import (
    InvocationDepthExceeded,
    invocation_depth_guard,
    run_subagent,
)
from primer.model.agent import Agent
from primer.model.chat import Tool, ToolCallResult, ToolExample
from primer.toolset._describe import make_tool
from primer.toolset._helpers import err as _err, ok as _ok
from primer.model.collection import Collection, Document
from primer.model.common import Identifiable
from primer.model.except_ import (
    ConflictError,
    PrimerError,
    NotFoundError,
)
from primer.model.graph import Graph, GraphThread
from primer.model.provider import (
    ArtifactStorageProvider,
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
)
from primer.model.tool_approval import ToolApprovalPolicy
from primer.model.workspace import (
    Workspace,
    WorkspaceChannelLink,
)
from primer.model.yield_ import ToolContext, Yielded
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


def _err_from_primer(exc: PrimerError, *, error_type: str) -> ToolCallResult:
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
# CRUD description helpers: self-contained create/update schemas + hint table
# ===========================================================================


@dataclass(frozen=True)
class _EntityHint:
    sample_id: str
    create_body: dict  # a MINIMAL VALID body for create/update examples


def _create_schema(model_cls: type) -> dict:
    """Self-contained JSON schema for {entity: <model>} (root-level $defs)."""
    wrapper = create_model(f"_Create_{model_cls.__name__}", entity=(model_cls, ...))
    return wrapper.model_json_schema()


def _update_schema(model_cls: type) -> dict:
    wrapper = create_model(
        f"_Update_{model_cls.__name__}", id=(str, ...), entity=(model_cls, ...)
    )
    return wrapper.model_json_schema()


_ENTITY_HINTS: dict[str, _EntityHint] = {
    "agent": _EntityHint(
        sample_id="code-reviewer",
        create_body={
            "id": "code-reviewer",
            "description": "Reviews diffs",
            "model": {"provider_id": "anthropic-1", "model_name": "claude-sonnet-4-6"},
        },
    ),
    "graph": _EntityHint(
        sample_id="incident-pipeline",
        create_body={
            "id": "incident-pipeline",
            "description": "Begin to End",
            "nodes": [
                {"kind": "begin", "id": "begin"},
                {"kind": "end", "id": "end"},
            ],
            "edges": [{"kind": "static", "from_node": "begin", "to_node": "end"}],
        },
    ),
    "collection": _EntityHint(
        sample_id="kb-1",
        create_body={
            "id": "kb-1",
            "description": "Knowledge base",
            "embedder": {"provider_id": "hf-1", "model": "all-MiniLM-L6-v2"},
            "search_provider_id": "ssp-1",
        },
    ),
    "llm_provider": _EntityHint(
        sample_id="anthropic-1",
        create_body={
            "id": "anthropic-1",
            "provider": "anthropic",
            "models": [{"name": "claude-sonnet-4-6", "context_length": 200000}],
            "config": {"api_key": "sk-x"},
            "limits": {"max_concurrency": 4},
        },
    ),
    "embedding_provider": _EntityHint(
        sample_id="hf-1",
        create_body={
            "id": "hf-1",
            "provider": "huggingface",
            "models": [{"name": "all-MiniLM-L6-v2"}],
            "config": {"token": "hf-x"},
            "limits": {"max_concurrency": 4},
        },
    ),
    "cross_encoder_provider": _EntityHint(
        sample_id="ce-1",
        create_body={
            "id": "ce-1",
            "provider": "huggingface",
            "models": [{"name": "BAAI/bge-reranker-v2-m3"}],
            "config": {"token": "hf-x"},
            "limits": {"max_concurrency": 4},
        },
    ),
    "semantic_search_provider": _EntityHint(
        sample_id="ssp-1",
        create_body={
            "id": "ssp-1",
            "provider": "pgvector",
            "config": {
                "hostname": "localhost",
                "username": "primer",
                "password": "secret",
                "database": "primer",
            },
        },
    ),
    "artifact_storage_provider": _EntityHint(
        sample_id="artifact-storage-1",
        create_body={
            "id": "artifact-storage-1",
            "provider": "db",
            "config": {},
        },
    ),
    "toolset": _EntityHint(
        sample_id="github-mcp",
        create_body={
            "id": "github-mcp",
            "provider": "mcp",
            "config": {
                "transport": "http",
                "config": {"url": "https://mcp.example.com"},
            },
        },
    ),
    "document": _EntityHint(
        sample_id="doc-1",
        create_body={
            "id": "doc-1",
            "collection_id": "kb-1",
            "name": "Onboarding guide",
        },
    ),
    "agent_thread": _EntityHint(
        sample_id="thread-1",
        create_body={
            "id": "thread-1",
            "agent_id": "code-reviewer",
            "created_at": "2026-01-01T00:00:00Z",
            "last_activity_at": "2026-01-01T00:00:00Z",
        },
    ),
    "graph_thread": _EntityHint(
        sample_id="gthread-1",
        create_body={
            "id": "gthread-1",
            "graph_id": "incident-pipeline",
            "created_at": "2026-01-01T00:00:00Z",
            "last_activity_at": "2026-01-01T00:00:00Z",
        },
    ),
    "tool_approval_policy": _EntityHint(
        sample_id="tap-1",
        create_body={
            "id": "tap-1",
            "toolset_id": "system",
            "tool_name": "delete_agent",
            "approval": {"type": "required"},
        },
    ),
    "channel_provider": _EntityHint(
        sample_id="slack-1",
        create_body={
            "id": "slack-1",
            "provider": "slack",
            "config": {"app_token": "xapp-x", "bot_token": "xoxb-x"},
        },
    ),
    "channel": _EntityHint(
        sample_id="chan-1",
        create_body={
            "id": "chan-1",
            "provider_id": "slack-1",
            "provider": "slack",
            "external_id": "C12345",
        },
    ),
}


def _hint(entity_label: str) -> _EntityHint:
    return _ENTITY_HINTS.get(
        entity_label,
        _EntityHint(sample_id=f"{entity_label}-1", create_body={"id": f"{entity_label}-1"}),
    )


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

    Create/update use a self-contained wrapper-model schema (built via
    ``_create_schema`` / ``_update_schema``) so the embedded ``$defs``
    resolve at the document root for validation, rather than a generic
    dict.
    """
    storage = storage_provider.get_storage(model_cls)
    cls_name = model_cls.__name__
    tools: dict[str, tuple[Tool, ToolHandler]] = {}
    hint = _hint(entity_label)

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
        except PrimerError as exc:
            return _err_from_primer(exc, error_type="storage-error")
        return _ok(response)

    tools[f"list_{entity_label_plural}"] = (
        make_tool(
            id=f"list_{entity_label_plural}",
            toolset_id=SYSTEM_TOOLSET_ID,
            purpose=(
                f"List {cls_name} rows as a page object (``items``, "
                "``length``, ``total``, ``next_cursor``)."
            ),
            when=(
                "Use when you need to browse or paginate this entity type; "
                f"not for one known id (use ``get_{entity_label}``) or a "
                f"predicate (use ``find_{entity_label_plural}``)."
            ),
            args_schema=_PaginationArgs.model_json_schema(),
            examples=[
                ToolExample(
                    args={"offset": 0, "limit": 20},
                    returns=f"a page of {entity_label_plural}",
                )
            ],
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
        make_tool(
            id=f"get_{entity_label}",
            toolset_id=SYSTEM_TOOLSET_ID,
            purpose=f"Fetch one {cls_name} by ``id``; returns the full entity.",
            when="Use when you know the exact id; returns ``type=not-found`` otherwise.",
            args_schema=_GetByIdArgs.model_json_schema(),
            examples=[
                ToolExample(args={"id": hint.sample_id}, returns=f"the {entity_label}")
            ],
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
        except (ConflictError, PrimerError) as exc:
            return _err_from_primer(exc, error_type="storage-error")
        if on_create is not None:
            await on_create(created.id)
        return _ok(created)

    tools[f"create_{entity_label}"] = (
        make_tool(
            id=f"create_{entity_label}",
            toolset_id=SYSTEM_TOOLSET_ID,
            purpose=(
                f"Create a new {cls_name} from the full ``entity`` body; "
                "the ``id`` must be unique."
            ),
            when=(
                "Use when adding a new row; duplicate id returns "
                "``type=conflict``, a bad body returns ``type=validation-error``."
            ),
            args_schema=_create_schema(model_cls),
            examples=[
                ToolExample(
                    args={"entity": hint.create_body},
                    returns=f"the stored {entity_label}",
                )
            ],
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
        if isinstance(body, dict) and not body.get("id"):
            body = {**body, "id": entity_id}
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
        except PrimerError as exc:
            return _err_from_primer(exc, error_type="storage-error")
        if on_update is not None:
            await on_update(updated.id)
        return _ok(updated)

    update_when = (
        "Use when overwriting a whole row; the body ``id`` must "
        "equal the path ``id``. Unknown id returns ``type=not-found``."
    )
    if on_update is not None:
        update_when += (
            " Mutating provider/toolset/vector-store rows invalidates the "
            "matching cached adapter immediately."
        )

    tools[f"update_{entity_label}"] = (
        make_tool(
            id=f"update_{entity_label}",
            toolset_id=SYSTEM_TOOLSET_ID,
            purpose=(
                f"Replace an existing {cls_name}; pass ``id`` and the full "
                "``entity`` (replaces, does not patch)."
            ),
            when=update_when,
            args_schema=_update_schema(model_cls),
            examples=[
                ToolExample(
                    args={"id": hint.sample_id, "entity": hint.create_body},
                    returns=f"the updated {entity_label}",
                )
            ],
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
        except PrimerError as exc:
            return _err_from_primer(exc, error_type="storage-error")
        if on_delete is not None:
            await on_delete(args.id)
        return _ok({"deleted": True, "id": args.id})

    delete_when = (
        "Use when removing a row; unknown id returns ``type=not-found``."
    )
    if on_delete is not None:
        delete_when += (
            " Deleting provider/toolset/vector-store rows invalidates the "
            "matching cached adapter immediately."
        )

    tools[f"delete_{entity_label}"] = (
        make_tool(
            id=f"delete_{entity_label}",
            toolset_id=SYSTEM_TOOLSET_ID,
            purpose=f"Delete a {cls_name} by ``id``; returns ``{{deleted, id}}``.",
            when=delete_when,
            args_schema=_DeleteByIdArgs.model_json_schema(),
            examples=[
                ToolExample(args={"id": hint.sample_id}, returns="deletion ack")
            ],
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
        except PrimerError as exc:
            return _err_from_primer(exc, error_type="storage-error")
        return _ok(response)

    tools[f"find_{entity_label_plural}"] = (
        make_tool(
            id=f"find_{entity_label_plural}",
            toolset_id=SYSTEM_TOOLSET_ID,
            purpose=(
                f"Find {cls_name} rows matching a predicate tree; same "
                f"pagination as ``list_{entity_label_plural}``."
            ),
            when=(
                "Use when filtering by field values; pass ``predicate=null`` "
                f"to match all (equivalent to ``list_{entity_label_plural}``)."
            ),
            args_schema=_FindArgs.model_json_schema(),
            examples=[
                ToolExample(
                    args={
                        "predicate": {
                            "kind": "predicate",
                            "left": {"kind": "field", "name": "id"},
                            "op": "=",
                            "right": {"kind": "value", "value": hint.sample_id},
                        }
                    },
                    returns="rows whose id equals the sample",
                )
            ],
        ),
        _find_handler,
    )

    if hint.create_body:
        model_cls.model_validate(hint.create_body)  # fail fast on a bad example body

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
            return _err_from_primer(exc, error_type="not-found")
        except PrimerError as exc:
            return _err_from_primer(exc, error_type="provider-error")
        return _ok({"models": list(models)})

    name = f"fetch_{label}_models"
    return name, (
        make_tool(
            id=name,
            toolset_id=SYSTEM_TOOLSET_ID,
            purpose=(
                f"Fetch the live model list reported by a configured "
                f"{pretty} provider."
            ),
            when=(
                "Use when you need the model identifiers the provider "
                "currently exposes; differs from the configured "
                "``models`` field on the provider row (what the app is "
                f"permitted to send) and from ``get_{label}`` (the stored "
                "config row)."
            ),
            args_schema=_ProviderIdArgs.model_json_schema(),
            examples=[
                ToolExample(
                    args={"provider_id": "anthropic-1"},
                    returns="the provider's available model ids",
                )
            ],
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
            return _err_from_primer(exc, error_type="not-found")
        except PrimerError as exc:
            return _err_from_primer(exc, error_type="provider-error")
        tools_out: list[dict[str, Any]] = []
        async for tool in provider.list_tools(principal=args.principal):
            tools_out.append(tool.model_dump(mode="json"))
        return _ok({"tools": tools_out})

    return "list_toolset_tools", (
        make_tool(
            id="list_toolset_tools",
            toolset_id=SYSTEM_TOOLSET_ID,
            purpose="Enumerate the tools a toolset currently exposes.",
            when=(
                "Use when you want to discover what a toolset offers "
                "before dispatching to it via ``call_tool``. Calls the "
                "live provider, so OAuth-protected MCP toolsets may return "
                "``is_error=true`` ``type=auth-required``."
            ),
            args_schema=_ToolsetIdArgs.model_json_schema(),
            examples=[
                ToolExample(
                    args={"toolset_id": "system"},
                    returns=(
                        "``{'tools': [Tool, ...]}`` where each Tool carries "
                        "its id, description, schema, and toolset_id"
                    ),
                )
            ],
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
            return _err_from_primer(exc, error_type="not-found")
        except PrimerError as exc:
            return _err_from_primer(exc, error_type="provider-error")
        try:
            result = await provider.call(
                tool_name=args.tool_name,
                arguments=args.arguments,
                principal=args.principal,
            )
        except PrimerError as exc:
            return _err_from_primer(exc, error_type="tool-call-error")
        return ToolCallResult(
            output=result.output,
            is_error=result.is_error,
            extended=result.extended,
        )

    return "call_tool", (
        make_tool(
            id="call_tool",
            toolset_id=SYSTEM_TOOLSET_ID,
            purpose="Meta-dispatch: invoke any tool from any toolset by id.",
            when=(
                "Use when you have discovered a tool via "
                "``list_toolset_tools`` and want to execute it without "
                "going through the dedicated agent toolset wiring. The "
                "dispatched tool's own ``output`` and ``is_error`` are "
                "passed through unchanged so you can act on them."
            ),
            args_schema=_CallToolArgs.model_json_schema(),
            examples=[
                ToolExample(
                    args={
                        "toolset_id": "misc",
                        "tool_name": "get_datetime",
                        "arguments": {},
                    },
                    returns="the dispatched tool's output and is_error",
                )
            ],
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
        make_tool(
            id="list_collection_documents",
            toolset_id=SYSTEM_TOOLSET_ID,
            purpose="List documents belonging to a collection.",
            when=(
                "Use when you want every document under a collection id "
                "(server-side filter on ``Document.collection_id``); same "
                "pagination contract as the entity ``list`` tools. Returns "
                "``type=not-found`` if the collection itself does not exist."
            ),
            args_schema=_CollectionDocumentsListArgs.model_json_schema(),
            examples=[
                ToolExample(
                    args={"collection_id": "kb-1", "limit": 20},
                    returns="a page of documents in the collection",
                )
            ],
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
        make_tool(
            id="find_collection_documents_by_meta",
            toolset_id=SYSTEM_TOOLSET_ID,
            purpose="Find documents in a collection by metadata equality.",
            when=(
                "Use when you want exact metadata matching (``meta_filter`` "
                "is a flat dict of ``{key: value}`` joined with AND); not "
                "for free-text relevance (use ``search_collection``) nor "
                "richer predicates like range/OR/IN (use ``find_documents`` "
                "with a hand-built Predicate)."
            ),
            args_schema=_CollectionFindByMetaArgs.model_json_schema(),
            examples=[
                ToolExample(
                    args={"collection_id": "kb-1", "meta_filter": {"source": "web"}},
                    returns="documents whose meta.source equals 'web'",
                )
            ],
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
        make_tool(
            id="search_collection",
            toolset_id=SYSTEM_TOOLSET_ID,
            purpose="Run semantic / hybrid search over a collection.",
            when=(
                "Use when you want free-text relevance ranking over "
                "document content; for exact metadata matching use "
                "``find_collection_documents_by_meta`` instead."
            ),
            args_schema=_CollectionSearchArgs.model_json_schema(),
            examples=[
                ToolExample(
                    args={"collection_id": "kb-1", "query": "onboarding"},
                    returns="ranked hits once the pipeline lands",
                    note=(
                        "STUB: returns ``is_error=true`` "
                        "``type=not-implemented`` until the SearchService "
                        "pipeline (embedder + vector store + optional "
                        "cross-encoder) is wired in the API layer."
                    ),
                )
            ],
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
        make_tool(
            id="refresh_collection",
            toolset_id=SYSTEM_TOOLSET_ID,
            purpose="Re-embed every document in a collection.",
            when=(
                "Use when you have swapped the embedder and want existing "
                "documents re-vectorised against the new model."
            ),
            args_schema=_CollectionIdArgs.model_json_schema(),
            examples=[
                ToolExample(
                    args={"collection_id": "kb-1"},
                    returns="a refresh acknowledgement once implemented",
                    note=(
                        "STUB: returns ``is_error=true`` "
                        "``type=not-implemented`` until the SearchService "
                        "ingestion pipeline lands."
                    ),
                )
            ],
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
        make_tool(
            id="get_document_content",
            toolset_id=SYSTEM_TOOLSET_ID,
            purpose="Fetch a document's text content.",
            when=(
                "Use when you need the raw text (looks up "
                "``Document.meta['content']`` where ``put_document`` "
                "stores it); empty-string content is normal for documents "
                "created without going through ``put_document``."
            ),
            args_schema=_DocumentIdArgs.model_json_schema(),
            examples=[
                ToolExample(
                    args={"document_id": "doc-1"},
                    returns="``{id, collection_id, name, content}``",
                )
            ],
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
        except PrimerError as exc:
            return _err_from_primer(exc, error_type="storage-error")
        return _ok(stored)

    out["put_document"] = (
        make_tool(
            id="put_document",
            toolset_id=SYSTEM_TOOLSET_ID,
            purpose="Ingest a document with content into a collection (upsert).",
            when=(
                "Use when you have raw text to store under a document; "
                "synthesises a ``Document`` row with ``content`` saved to "
                "``meta['content']`` and creates or updates by id. Until "
                "the chunking + embedding pipeline lands this does NOT "
                "vectorise the document (search will not see it); use "
                "``create_document`` / ``update_document`` for raw "
                "row-level CRUD without content semantics."
            ),
            args_schema=_PutDocumentArgs.model_json_schema(),
            examples=[
                ToolExample(
                    args={
                        "id": "doc-1",
                        "collection_id": "kb-1",
                        "name": "Onboarding Guide",
                        "content": "Welcome to the team.",
                    },
                    returns="the stored Document row",
                )
            ],
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
        ("artifact_storage_provider", "artifact_storage_providers", ArtifactStorageProvider, None, None, None),
        ("tool_approval_policy", "tool_approval_policies", ToolApprovalPolicy, None, None, None),
        ("channel_provider", "channel_providers", ChannelProvider, None, None, None),
        ("channel", "channels", Channel, None, None, None),
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
        make_tool(
            id="invalidate_semantic_search_provider",
            toolset_id=SYSTEM_TOOLSET_ID,
            purpose=(
                "Expire the cached VectorStoreProvider adapter for a "
                "SemanticSearchProvider row."
            ),
            when=(
                "Use when you have updated the provider row and want the "
                "next search request to rebuild the adapter from the new "
                "config; safe to call even if no cached instance exists "
                "(no-op)."
            ),
            args_schema=_InvalidateSSPArgs.model_json_schema(),
            examples=[
                ToolExample(
                    args={"id": "ssp-1"},
                    returns="``{'invalidated': true, 'id': '...'}``",
                )
            ],
        ),
        _invalidate_ssp_handler,
    )

    # ---- Workspace channel-association tools -------------------------
    class _SetWorkspaceChannelAssociationArgs(BaseModel):
        workspace_id: str = Field(
            ..., min_length=1, description="Id of the Workspace to update."
        )
        channel_id: str = Field(
            ..., min_length=1, description="Id of the Channel to associate."
        )

    _workspace_storage = storage_provider.get_storage(Workspace)
    _channel_storage = storage_provider.get_storage(Channel)

    async def _set_workspace_channel_association_handler(
        arguments: dict[str, Any],
    ) -> ToolCallResult:
        try:
            args = _SetWorkspaceChannelAssociationArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        ws = await _workspace_storage.get(args.workspace_id)
        if ws is None:
            return _err(
                f"Workspace {args.workspace_id!r} does not exist",
                error_type="not-found",
            )
        channel = await _channel_storage.get(args.channel_id)
        if channel is None:
            return _err(
                f"Channel {args.channel_id!r} does not exist",
                error_type="not-found",
            )
        updated = ws.model_copy(
            update={
                "channel_association": WorkspaceChannelLink(
                    channel_id=args.channel_id
                )
            }
        )
        try:
            await _workspace_storage.update(updated)
        except PrimerError as exc:
            return _err_from_primer(exc, error_type="storage-error")
        return _ok(
            {
                "ok": True,
                "workspace_id": args.workspace_id,
                "channel_id": args.channel_id,
            }
        )

    registry["set_workspace_channel_association"] = (
        make_tool(
            id="set_workspace_channel_association",
            toolset_id=SYSTEM_TOOLSET_ID,
            purpose=(
                "Associate a Channel with a Workspace so that session "
                "gates (ask_user / tool_approval) forward to that channel."
            ),
            when=(
                "Use when you want session gates on a workspace to notify "
                "a Slack / Telegram / Discord channel; pass both ids and "
                "the association is stored on the Workspace row. Returns "
                "``type=not-found`` for unknown workspace or channel."
            ),
            args_schema=_SetWorkspaceChannelAssociationArgs.model_json_schema(),
            examples=[
                ToolExample(
                    args={"workspace_id": "ws-1", "channel_id": "chan-1"},
                    returns="``{ok: true, workspace_id, channel_id}``",
                )
            ],
        ),
        _set_workspace_channel_association_handler,
    )

    class _ClearWorkspaceChannelAssociationArgs(BaseModel):
        workspace_id: str = Field(
            ..., min_length=1, description="Id of the Workspace to update."
        )

    async def _clear_workspace_channel_association_handler(
        arguments: dict[str, Any],
    ) -> ToolCallResult:
        try:
            args = _ClearWorkspaceChannelAssociationArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        ws = await _workspace_storage.get(args.workspace_id)
        if ws is None:
            return _err(
                f"Workspace {args.workspace_id!r} does not exist",
                error_type="not-found",
            )
        updated = ws.model_copy(update={"channel_association": None})
        try:
            await _workspace_storage.update(updated)
        except PrimerError as exc:
            return _err_from_primer(exc, error_type="storage-error")
        return _ok({"ok": True, "workspace_id": args.workspace_id})

    registry["clear_workspace_channel_association"] = (
        make_tool(
            id="clear_workspace_channel_association",
            toolset_id=SYSTEM_TOOLSET_ID,
            purpose=(
                "Remove the channel association from a Workspace so that "
                "session gates are no longer forwarded to any channel."
            ),
            when=(
                "Use when you want to detach the channel from a workspace; "
                "safe to call even if no association is set (no-op). "
                "Returns ``type=not-found`` for an unknown workspace."
            ),
            args_schema=_ClearWorkspaceChannelAssociationArgs.model_json_schema(),
            examples=[
                ToolExample(
                    args={"workspace_id": "ws-1"},
                    returns="``{ok: true, workspace_id}``",
                )
            ],
        ),
        _clear_workspace_channel_association_handler,
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

    # ---- Dynamic invocation: invoke_agent ----------------------------
    class _InvokeAgentArgs(BaseModel):
        agent_id: str = Field(..., min_length=1, description="Agent to run.")
        prompt: str = Field(
            ..., min_length=1, description="Input for the subagent."
        )

    async def _invoke_agent_handler(
        arguments: dict[str, Any],
    ) -> ToolCallResult:
        try:
            args = _InvokeAgentArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        try:
            with invocation_depth_guard():
                text = await run_subagent(
                    agent_id=args.agent_id,
                    prompt=args.prompt,
                    storage_provider=storage_provider,
                    provider_registry=provider_registry,
                )
        except InvocationDepthExceeded as exc:
            return _err(
                f"invocation depth exceeded: {exc}", error_type="bad-request"
            )
        except ValueError as exc:
            return _err(str(exc), error_type="bad-request")
        return _ok({"output": text})

    registry["invoke_agent"] = (
        make_tool(
            id="invoke_agent",
            toolset_id=toolset_id,
            purpose=(
                "Run another agent once on a prompt and get its text back "
                "(subagent). Returns ``{output: <text>}``."
            ),
            when=(
                "Use when you want a specialised agent to handle a "
                "self-contained subtask and return a result; not for handing "
                "the whole conversation off (use ``switch_to_agent``)."
            ),
            args_schema=_InvokeAgentArgs.model_json_schema(),
            examples=[
                ToolExample(
                    args={
                        "agent_id": "agent-researcher",
                        "prompt": "Summarise the RFC.",
                    },
                    returns="``{output: <summary>}``",
                    note="blocking subagent",
                ),
            ],
        ),
        _invoke_agent_handler,
    )

    # ---- Dynamic invocation: switch_to_agent (chat-only handoff) -----
    class _SwitchToAgentArgs(BaseModel):
        agent_id: str = Field(
            ..., min_length=1, description="Agent to hand off to."
        )
        prompt: str = Field(
            ..., min_length=1, description="Handoff instruction for the new agent."
        )

    async def _switch_to_agent_handler(
        arguments: dict[str, Any], *, ctx: ToolContext,
    ) -> ToolCallResult | Yielded:
        try:
            args = _SwitchToAgentArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        if ctx.chat_id is None or ctx.session_id is not None:
            return _err(
                "switch_to_agent is only available in chats (not workspace "
                "sessions)",
                error_type="bad-request",
            )
        agents = storage_provider.get_storage(Agent)
        if await agents.get(args.agent_id) is None:
            return _err(
                f"agent {args.agent_id!r} does not exist",
                error_type="not-found",
            )
        return Yielded(
            tool_name="",  # provider stamps "switch_to_agent"
            event_key=f"switch_to_agent:{ctx.chat_id}:{ctx.tool_call_id}",
            resume_metadata={"agent_id": args.agent_id, "prompt": args.prompt},
        )

    registry["switch_to_agent"] = (
        make_tool(
            id="switch_to_agent",
            toolset_id=toolset_id,
            purpose=(
                "Hand the current chat off to another agent with a prompt; "
                "the new agent takes over."
            ),
            when=(
                "Use when you want to delegate the rest of THIS conversation "
                "to another agent; for a one-off subtask use invoke_agent."
            ),
            args_schema=_SwitchToAgentArgs.model_json_schema(),
            examples=[
                ToolExample(
                    args={
                        "agent_id": "agent-coder",
                        "prompt": "Implement the plan above.",
                    },
                    returns="(turn handed off)",
                    note="chat-only; ends the turn",
                ),
            ],
            yields=True,
            requires_session=True,
        ),
        _switch_to_agent_handler,
    )

    logger.info(
        "system toolset assembled with %d tools (id=%s)",
        len(registry),
        toolset_id,
    )

    return InternalToolsetProvider(toolset_id=toolset_id, registry=registry)


__all__ = ["SYSTEM_TOOLSET_ID", "build_system_toolset"]
