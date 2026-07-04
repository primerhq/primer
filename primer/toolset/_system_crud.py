"""Generic CRUD-tool generators and entity extras for the ``system`` toolset.

Split out of :mod:`primer.toolset.system` (a god-module decomposition). This
module holds the *generative* surface - the per-entity CRUD factory
(``_crud_tools_for``), its example-body hint table, and the entity-specific
extra builders (provider ``fetch_models``, toolset ``list``/``call_tool``,
collection search/list, document content/path tools). ``build_system_toolset``
in ``system.py`` wires these into one provider; the generated tool ids (all
f-string-built) are unchanged by the move.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, ValidationError, create_model

from primer.agent.approval import (
    ApprovalContext,
    ApprovalResolver,
    evaluate_approval_gate,
)
from primer.model.chat import Tool, ToolCallResult, ToolExample
from primer.toolset._describe import make_tool
from primer.toolset._helpers import ok as _ok
from primer.model.collection import Collection, Document
from primer.model.common import Identifiable
from primer.model.except_ import (
    ConflictError,
    PrimerError,
    NotFoundError,
)
from primer.model.storage import (
    Predicate,
)
from primer.model.yield_ import ToolContext, Yielded, YieldToWorker
from primer.toolset._system_common import (
    SYSTEM_TOOLSET_ID,
    _DeleteByIdArgs,
    _err_from_primer,
    _err_from_validation,
    _FindArgs,
    _GetByIdArgs,
    _PaginationArgs,
    _parse_order_by,
    _parse_page,
)
from primer.toolset._helpers import err as _err
from primer.toolset.internal import ToolHandler


if TYPE_CHECKING:
    from primer.api.registries import ProviderRegistry
    from primer.api.registries.semantic_search_registry import SemanticSearchRegistry
    from primer.int.storage_provider import StorageProvider
    from primer.knowledge.document_service import DocumentService


logger = logging.getLogger("primer.toolset.system")


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
            "path": "doc-1.md",
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
    required_role: str | None = None,
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
            required_role=required_role,
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
            required_role=required_role,
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
            required_role=required_role,
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
            required_role=required_role,
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
            required_role=required_role,
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
            required_role=required_role,
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
    approval_resolver: "ApprovalResolver | None" = None,
) -> tuple[str, tuple[Tool, ToolHandler]]:
    async def _handler(
        arguments: dict[str, Any], *, ctx: ToolContext | None = None,
    ) -> ToolCallResult | Yielded:
        try:
            args = _CallToolArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)

        # Approval gate - enforced BEFORE dispatch so a gated tool invoked
        # through this meta-dispatch path cannot bypass the operator's
        # ToolApprovalPolicy. Mirrors the agent-loop dispatch gate
        # (primer.agent.tool_manager): resolve the policy for the INNER
        # (toolset_id, tool_name), evaluate it, and on a "required" verdict
        # park for approval by yielding ``_approval`` exactly as the agent
        # loop does. The resume path re-dispatches the inner tool via its
        # owning toolset provider (see _resume_call_tool_dispatch) on
        # approve, or returns an error on reject/timeout/cancel.
        #
        # ``ctx`` is None only when there is no session/chat to park onto
        # (e.g. an out-of-loop dispatch). Without a park surface we cannot
        # safely run a gated tool, so fail closed with an error rather than
        # bypass the gate.
        if approval_resolver is not None and ctx is None:
            policy = await approval_resolver.find(
                toolset_id=args.toolset_id, tool_name=args.tool_name,
            )
            if policy is not None and policy.enabled:
                return _err(
                    f"tool {args.tool_name!r} in toolset "
                    f"{args.toolset_id!r} requires approval but there is "
                    "no session or chat to park for it; invoke it through "
                    "an agent session or chat.",
                    error_type="approval-required",
                )
        if approval_resolver is not None and ctx is not None:
            policy = await approval_resolver.find(
                toolset_id=args.toolset_id, tool_name=args.tool_name,
            )
            if policy is not None and policy.enabled:
                approval_ctx = ApprovalContext(
                    tool_name=args.tool_name,
                    toolset_id=args.toolset_id,
                    arguments=args.arguments or {},
                    agent_id=None,
                    session_id=ctx.session_id,
                    chat_id=ctx.chat_id,
                    requested_at=datetime.now(UTC),
                )
                verdict = await evaluate_approval_gate(
                    policy=policy,
                    context=approval_ctx,
                    provider_registry=registry,
                )
                if verdict.required:
                    session_or_chat = (
                        ctx.session_id or ctx.chat_id or "unknown"
                    )
                    # Raise YieldToWorker directly (rather than returning a
                    # Yielded sentinel) so the parked tool_name stays
                    # ``_approval``: the InternalToolsetProvider would
                    # otherwise re-stamp a returned Yielded with this tool's
                    # own name (``call_tool``), and the worker resume path
                    # keys the approval re-dispatch on ``_approval``. This is
                    # exactly how the agent loop parks for approval.
                    raise YieldToWorker(
                        Yielded(
                            tool_name="_approval",
                            event_key=(
                                f"tool_approval:{session_or_chat}:"
                                f"{ctx.tool_call_id}"
                            ),
                            timeout=policy.timeout_seconds,
                            resume_metadata={
                                "policy_id": policy.id,
                                "approval_type": policy.approval.type.value,
                                "gate_reason": verdict.reason,
                                # Inner call re-dispatched via the owning
                                # toolset provider on approve (not the agent
                                # tool surface, which may not list this tool).
                                "via_call_tool": {
                                    "toolset_id": args.toolset_id,
                                    "principal": args.principal,
                                },
                                "original_call": {
                                    "id": ctx.tool_call_id,
                                    "name": args.tool_name,
                                    "arguments": args.arguments or {},
                                },
                            },
                        ),
                        tool_call_id=ctx.tool_call_id,
                    )

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
                "passed through unchanged so you can act on them. If the "
                "dispatched tool has an approval policy, this call parks "
                "for approval just like a normal agent tool call."
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
            yields=True,
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
    provider_registry: "ProviderRegistry",
    semantic_search_registry: "SemanticSearchRegistry | None" = None,
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

    # ---- search_collection --------------------------------------------
    async def _search(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _CollectionSearchArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        if semantic_search_registry is None:
            return _err(
                "semantic search is unavailable: no SemanticSearchRegistry "
                "wired into this process; use "
                "``find_collection_documents_by_meta`` for metadata "
                "filtering instead.",
                error_type="unavailable",
            )
        coll = await collections.get(args.collection_id)
        if coll is None:
            return _err(
                f"Collection {args.collection_id!r} does not exist",
                error_type="not-found",
            )
        # Mirror POST /v1/collections/{id}/search (the console / SSP path):
        # vectorise the query with the collection's OWN embedder so query
        # and index vectors share dimensionality + metric, then run the
        # similarity search against the collection's vector store, resolved
        # via the collection's search_provider_id.
        from primer.model.chat import TextPart
        from primer.model.except_ import BadRequestError
        from primer.search.run import run_collection_search

        try:
            embedder = await provider_registry.get_embedder(
                coll.embedder.provider_id
            )
            response = await embedder.embed(
                model=coll.embedder.model,
                inputs=[TextPart(text=args.query)],
            )
            vector = list(response.embeddings[0].vector)
            store = await semantic_search_registry.get_store(
                coll.search_provider_id
            )
        except NotFoundError as exc:
            return _err_from_primer(exc, error_type="not-found")
        except PrimerError as exc:
            return _err_from_primer(exc, error_type="provider-error")
        # SSP registration is lazy: a collection with Document rows but no
        # indexed vectors yet is unknown to the store catalogue and search
        # raises BadRequestError("...is not registered..."). Treat that as
        # "nothing indexed yet" -> empty hits (matches the REST route).
        #
        # run_collection_search applies the collection's `search` config
        # (cross-encoder rerank + MMR) when set, reusing the query vector
        # we already embedded so the no-config path does not double-embed.
        try:
            hits = await run_collection_search(
                collection=coll,
                embedder=embedder,
                store=store,
                query=args.query,
                top_k=args.top_k,
                cross_encoder_resolver=provider_registry,
                query_vector=vector,
            )
        except BadRequestError as exc:
            if "is not registered" not in str(exc):
                return _err_from_primer(exc, error_type="search-error")
            hits = []
        except PrimerError as exc:
            return _err_from_primer(exc, error_type="search-error")
        return _ok(
            {
                "hits": [
                    {
                        "document_id": h.record.document_id,
                        "chunk_id": h.record.chunk_id,
                        "score": h.score,
                        "text": h.record.text,
                        "meta": h.record.meta,
                    }
                    for h in hits
                ],
            }
        )

    out["search_collection"] = (
        make_tool(
            id="search_collection",
            toolset_id=SYSTEM_TOOLSET_ID,
            purpose="Run semantic search over a collection's document contents.",
            when=(
                "Use when you want free-text relevance ranking over "
                "document content; for exact metadata matching use "
                "``find_collection_documents_by_meta`` instead. Returns "
                "ranked chunk hits ``{document_id, chunk_id, score, text, "
                "meta}`` scoped to the collection."
            ),
            args_schema=_CollectionSearchArgs.model_json_schema(),
            examples=[
                ToolExample(
                    args={"collection_id": "kb-1", "query": "onboarding"},
                    returns=(
                        "``{'hits': [{document_id, chunk_id, score, text, "
                        "meta}, ...]}`` ranked most-relevant first; an empty "
                        "list when nothing is indexed yet"
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


class _GetDocumentArgs(BaseModel):
    """Address a document by its path within a collection."""

    collection_id: str = Field(
        ..., min_length=1, description="Parent collection id."
    )
    path: str = Field(
        ...,
        min_length=1,
        description="Document path within the collection, e.g. ``concepts/slo.md``.",
    )


class _PutDocumentArgs(BaseModel):
    """Upsert a document body at a path within a collection.

    The body is written to the content store (addressed by
    ``(collection_id, path)``), NOT to ``Document.meta``. Repeated puts on
    the same path update the same entity. When the collection has search on,
    the write is followed by a best-effort re-index.
    """

    collection_id: str = Field(
        ..., min_length=1, description="Parent collection id."
    )
    path: str = Field(
        ...,
        min_length=1,
        description="Document path within the collection, e.g. ``concepts/slo.md``.",
    )
    content: str = Field(
        ...,
        description=(
            "Raw text body, stored in the content store keyed by path. "
            "An empty string is allowed (creates or clears a document body)."
        ),
    )
    title: str | None = Field(
        default=None,
        description=(
            "Optional human-readable title; defaults to the path's final "
            "segment when omitted."
        ),
    )
    meta: dict[str, Any] | None = Field(
        default=None,
        description="Optional application-defined metadata stored on the entity.",
    )


class _ListDocumentsArgs(BaseModel):
    """List documents under an optional path prefix (no bodies)."""

    collection_id: str = Field(
        ..., min_length=1, description="Parent collection id."
    )
    prefix: str | None = Field(
        default=None,
        description=(
            "Optional path prefix filter, e.g. ``concepts/``. Omit to list "
            "every document in the collection."
        ),
    )


class _MoveDocumentArgs(BaseModel):
    """Move a document from one path to another within a collection."""

    collection_id: str = Field(
        ..., min_length=1, description="Parent collection id."
    )
    src: str = Field(
        ..., min_length=1, alias="from", description="Current document path."
    )
    dst: str = Field(
        ..., min_length=1, alias="to", description="Destination document path."
    )

    model_config = {"populate_by_name": True}


def _document_service_factory(
    *,
    storage_provider: "StorageProvider",
    provider_registry: "ProviderRegistry",
    semantic_search_registry: "SemanticSearchRegistry | None",
) -> "Callable[[], DocumentService]":
    """Return a lazily-memoised builder for the toolset's :class:`DocumentService`.

    Construction is deferred to first use so building the system toolset over
    a storage provider that has no content store (in-memory unit-test fakes)
    does not touch ``get_content_store`` / ``transaction`` until a document
    tool is actually invoked.

    Mirrors :func:`primer.api.deps.get_document_service`: when a
    SemanticSearchRegistry is wired (search on) the service gets a
    best-effort indexer that re-embeds the body AFTER the atomic entity +
    content write commits, so a ``put_document`` into a search-on collection
    still indexes the document. With no registry (search off / unit tests)
    the indexer is ``None`` and ``put_document`` is a pure storage write.
    """
    cached: dict[str, "DocumentService"] = {}

    def _build() -> "DocumentService":
        if "svc" in cached:
            return cached["svc"]
        from primer.knowledge.document_service import DocumentService

        indexer = None
        if semantic_search_registry is not None:
            from primer.knowledge.indexing import index_document

            async def indexer(*, document: Document, content: str) -> None:  # noqa: F811
                collection = await storage_provider.get_storage(Collection).get(
                    document.collection_id
                )
                if collection is None:
                    return
                try:
                    await index_document(
                        document=document,
                        collection=collection,
                        provider_registry=provider_registry,
                        semantic_search_registry=semantic_search_registry,
                        content_store=storage_provider.get_content_store(),
                    )
                except Exception:  # noqa: BLE001 - best-effort indexing
                    logger.exception(
                        "document %s: indexing failed; row persisted but not "
                        "searchable",
                        document.id,
                    )

        svc = DocumentService(storage_provider, indexer=indexer)
        cached["svc"] = svc
        return svc

    return _build


def _document_extras(
    *,
    service_factory: "Callable[[], DocumentService]",
) -> dict[str, tuple[Tool, ToolHandler]]:
    out: dict[str, tuple[Tool, ToolHandler]] = {}

    async def _get_content(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _GetDocumentArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        try:
            res = await service_factory().read(
                collection_id=args.collection_id, path=args.path
            )
        except NotFoundError as exc:
            return _err_from_primer(exc, error_type="not-found")
        except PrimerError as exc:
            return _err_from_primer(exc, error_type="storage-error")
        return _ok(
            {
                "id": res.document.id,
                "collection_id": res.document.collection_id,
                "path": res.document.path,
                "title": res.document.title,
                "content": res.content,
            }
        )

    out["get_document_content"] = (
        make_tool(
            id="get_document_content",
            toolset_id=SYSTEM_TOOLSET_ID,
            purpose="Fetch a document's text content by path.",
            when=(
                "Use when you need the raw body of a document addressed by "
                "``(collection_id, path)``; the body is read from the content "
                "store. Returns ``type=not-found`` if no document lives at "
                "that path."
            ),
            args_schema=_GetDocumentArgs.model_json_schema(),
            examples=[
                ToolExample(
                    args={"collection_id": "kb-1", "path": "concepts/slo.md"},
                    returns="``{id, collection_id, path, title, content}``",
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
        try:
            doc = await service_factory().upsert(
                collection_id=args.collection_id,
                path=args.path,
                content=args.content,
                title=args.title,
                meta=args.meta,
            )
        except ConflictError as exc:
            return _err_from_primer(exc, error_type="conflict")
        except PrimerError as exc:
            return _err_from_primer(exc, error_type="storage-error")
        return _ok(doc)

    out["put_document"] = (
        make_tool(
            id="put_document",
            toolset_id=SYSTEM_TOOLSET_ID,
            purpose="Upsert a document body at a path within a collection.",
            when=(
                "Use when you have raw text to store at a path; the body is "
                "written to the content store addressed by "
                "``(collection_id, path)`` (not ``meta``) and the entity is "
                "created or replaced at that path. When the collection has "
                "search on, the document is re-indexed best-effort after the "
                "write. Use ``create_document`` / ``update_document`` for raw "
                "row-level CRUD without content semantics."
            ),
            args_schema=_PutDocumentArgs.model_json_schema(),
            examples=[
                ToolExample(
                    args={
                        "collection_id": "kb-1",
                        "path": "onboarding.md",
                        "content": "Welcome to the team.",
                        "title": "Onboarding Guide",
                    },
                    returns="the stored Document entity",
                )
            ],
        ),
        _put_document,
    )

    async def _list_documents(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _ListDocumentsArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        try:
            entries = await service_factory().list(
                collection_id=args.collection_id, prefix=args.prefix
            )
        except PrimerError as exc:
            return _err_from_primer(exc, error_type="storage-error")
        return _ok(
            {
                "documents": [
                    {"path": e.path, "document_id": e.document_id, "size": e.size}
                    for e in entries
                ]
            }
        )

    out["list_documents"] = (
        make_tool(
            id="list_documents",
            toolset_id=SYSTEM_TOOLSET_ID,
            purpose="List a collection's documents by path (no bodies).",
            when=(
                "Use when you want the path hierarchy of a collection; pass "
                "an optional ``prefix`` to scope to a subtree. Returns "
                "``{documents: [{path, document_id, size}]}`` without loading "
                "any body; use ``get_document_content`` to read a body."
            ),
            args_schema=_ListDocumentsArgs.model_json_schema(),
            examples=[
                ToolExample(
                    args={"collection_id": "kb-1", "prefix": "concepts/"},
                    returns="``{documents: [{path, document_id, size}, ...]}``",
                )
            ],
        ),
        _list_documents,
    )

    async def _move_document(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _MoveDocumentArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        try:
            await service_factory().move(
                collection_id=args.collection_id, src=args.src, dst=args.dst
            )
        except NotFoundError as exc:
            return _err_from_primer(exc, error_type="not-found")
        except ConflictError as exc:
            return _err_from_primer(exc, error_type="conflict")
        except PrimerError as exc:
            return _err_from_primer(exc, error_type="storage-error")
        return _ok(
            {
                "moved": True,
                "collection_id": args.collection_id,
                "from": args.src,
                "to": args.dst,
            }
        )

    out["move_document"] = (
        make_tool(
            id="move_document",
            toolset_id=SYSTEM_TOOLSET_ID,
            purpose="Move a document from one path to another within a collection.",
            when=(
                "Use when you want to rename or relocate a document; the same "
                "entity keeps its id and body, only the path changes. Returns "
                "``type=not-found`` if ``from`` does not exist and "
                "``type=conflict`` if ``to`` is already taken."
            ),
            args_schema=_MoveDocumentArgs.model_json_schema(),
            examples=[
                ToolExample(
                    args={
                        "collection_id": "kb-1",
                        "from": "draft.md",
                        "to": "concepts/final.md",
                    },
                    returns="``{moved: true, collection_id, from, to}``",
                )
            ],
        ),
        _move_document,
    )

    return out
