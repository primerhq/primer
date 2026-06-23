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
* Document extras - ``get_document_content``, ``put_document``,
  ``list_documents``, ``move_document`` (all path-addressed; bodies live
  in the content store).

Total: ~75 tools. ``search_collection`` runs real semantic search over
a collection's indexed document contents (same embedder + vector-store
path the console / ``POST /v1/collections/{id}/search`` route uses).
``refresh_collection`` is stubbed with ``is_error=True`` until the
SearchService ingestion pipeline lands.

Cascade invalidation
--------------------

Mutations on rows backed by a cached adapter (LLMProvider,
EmbeddingProvider, CrossEncoderProvider, Toolset, VectorStoreConfig)
invoke the matching ``invalidate_*`` on the registry so the next
read/call rebuilds the adapter from the new row.

Module layout
-------------

This module is a thin facade over the decomposed implementation:

* :mod:`primer.toolset._system_common` - shared error wrappers,
  argument models, and the page/order-by parsers.
* :mod:`primer.toolset._system_crud` - the generic CRUD-tool factory
  (``_crud_tools_for``), its example-body hint table, and the
  entity-specific extra builders (fetch_models, toolset list/call_tool,
  collection, document).
* :mod:`primer.toolset._system_tools` - the hand-written ``ask_user``
  tool (model, resume hook, handler).

``build_system_toolset`` (below) wires those into one provider and adds
the bespoke tools that close over its per-build dependencies
(reply-binding, channel-binding, invoke_agent, switch_to_agent,
ask_user). Everything historically imported as
``from primer.toolset.system import X`` is re-exported here unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, ValidationError

from primer.agent.approval import ApprovalResolver
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
from primer.model.except_ import (
    PrimerError,
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
from primer.model.thread import Thread
from primer.model.channel import (
    Channel,
    ChannelProvider,
)
from primer.model.event_matcher import EventMatcher
from primer.channel.reply_binding import ReplyTarget
from primer.model.trigger import SubscriptionConfig
from primer.trigger.service import (
    ServiceDeps,
    SubscriptionNotFound,
    TriggerNotFound,
    create_subscription,
    delete_subscription,
    get_trigger,
    list_subscriptions,
)
from primer.model.tool_approval import ToolApprovalPolicy
from primer.model.workspace import (
    Workspace,
    WorkspaceChannelLink,
)
from primer.model.yield_ import ToolContext, Yielded
from primer.toolset.internal import InternalToolsetProvider, ToolHandler

# Re-exported helpers / argument models / parsers (shared surface).
from primer.toolset._system_common import (
    SYSTEM_TOOLSET_ID,
    logger,
    _DeleteByIdArgs,
    _err_from_primer,
    _err_from_validation,
    _FindArgs,
    _GetByIdArgs,
    _PaginationArgs,
    _parse_order_by,
    _parse_page,
)

# Re-exported CRUD generators + entity extras.
from primer.toolset._system_crud import (
    _CallToolArgs,
    _CollectionDocumentsListArgs,
    _CollectionFindByMetaArgs,
    _CollectionIdArgs,
    _CollectionSearchArgs,
    _create_schema,
    _crud_tools_for,
    _document_extras,
    _document_service_factory,
    _EntityHint,
    _ENTITY_HINTS,
    _fetch_models_tool,
    _GetDocumentArgs,
    _hint,
    _ListDocumentsArgs,
    _list_toolset_tools_tool,
    _call_tool_tool,
    _collection_extras,
    _MoveDocumentArgs,
    _ProviderIdArgs,
    _PutDocumentArgs,
    _ToolsetIdArgs,
    _update_schema,
)

# Re-exported hand-written tools.
from primer.toolset._system_tools import (
    _AskUserArgs,
    _ask_user_handler,
    ask_user_resume,
)


if TYPE_CHECKING:
    from primer.api.registries import ProviderRegistry
    from primer.api.registries.semantic_search_registry import SemanticSearchRegistry
    from primer.int.storage_provider import StorageProvider


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

    # ---- Workspace reply-binding tools -------------------------------
    class _SetReplyBindingArgs(BaseModel):
        workspace_id: str = Field(
            ..., min_length=1, description="Id of the Workspace to update."
        )
        channel_id: str = Field(
            ..., min_length=1, description="Id of the Channel to bind replies to."
        )
        anchor: str | None = Field(
            default=None,
            description=(
                "Optional standing room anchor (e.g. a Slack thread ts) the "
                "workspace's outbound replies attach to. Omit to post to the "
                "channel root."
            ),
        )

    _workspace_storage = storage_provider.get_storage(Workspace)
    _channel_storage = storage_provider.get_storage(Channel)

    async def _set_reply_binding_handler(
        arguments: dict[str, Any],
    ) -> ToolCallResult:
        try:
            args = _SetReplyBindingArgs.model_validate(arguments)
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
                "reply_binding": WorkspaceChannelLink(
                    channel_id=args.channel_id,
                    anchor=args.anchor,
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
                "anchor": args.anchor,
            }
        )

    registry["set_reply_binding"] = (
        make_tool(
            id="set_reply_binding",
            toolset_id=SYSTEM_TOOLSET_ID,
            purpose=(
                "Set the standing workspace reply binding so that session "
                "traffic (gates / inform / lifecycle / final result) replies "
                "to a channel."
            ),
            when=(
                "Use when you want a workspace's session traffic to reply to "
                "a Slack / Telegram / Discord channel; pass both ids (and an "
                "optional ``anchor``) and the reply binding is stored on the "
                "Workspace row. Returns ``type=not-found`` for unknown "
                "workspace or channel."
            ),
            args_schema=_SetReplyBindingArgs.model_json_schema(),
            examples=[
                ToolExample(
                    args={"workspace_id": "ws-1", "channel_id": "chan-1"},
                    returns="``{ok: true, workspace_id, channel_id, anchor}``",
                )
            ],
        ),
        _set_reply_binding_handler,
    )

    class _ClearReplyBindingArgs(BaseModel):
        workspace_id: str = Field(
            ..., min_length=1, description="Id of the Workspace to update."
        )

    async def _clear_reply_binding_handler(
        arguments: dict[str, Any],
    ) -> ToolCallResult:
        try:
            args = _ClearReplyBindingArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        ws = await _workspace_storage.get(args.workspace_id)
        if ws is None:
            return _err(
                f"Workspace {args.workspace_id!r} does not exist",
                error_type="not-found",
            )
        updated = ws.model_copy(update={"reply_binding": None})
        try:
            await _workspace_storage.update(updated)
        except PrimerError as exc:
            return _err_from_primer(exc, error_type="storage-error")
        return _ok({"ok": True, "workspace_id": args.workspace_id})

    registry["clear_reply_binding"] = (
        make_tool(
            id="clear_reply_binding",
            toolset_id=SYSTEM_TOOLSET_ID,
            purpose=(
                "Clear the standing workspace reply binding so that session "
                "traffic is no longer forwarded to any channel."
            ),
            when=(
                "Use when you want to detach the channel from a workspace; "
                "safe to call even if no reply binding is set (no-op). "
                "Returns ``type=not-found`` for an unknown workspace."
            ),
            args_schema=_ClearReplyBindingArgs.model_json_schema(),
            examples=[
                ToolExample(
                    args={"workspace_id": "ws-1"},
                    returns="``{ok: true, workspace_id}``",
                )
            ],
        ),
        _clear_reply_binding_handler,
    )

    # ---- Inbound channel-binding (Subscription) tools ----------------
    # A "binding" is a Subscription on a ``channel`` trigger: a normalized
    # event matcher -> platform action. These delegate to the same
    # primer.trigger.service mutation path the REST router uses, so the
    # toolset stays a thin wrapper.
    class _CreateChannelBindingArgs(BaseModel):
        trigger_id: str = Field(
            ..., min_length=1, description="Id of the channel trigger to bind to."
        )
        event_matcher: EventMatcher | None = Field(
            default=None,
            description=(
                "Predicate gating which channel events fire this binding "
                "(AND of present fields). Omit to match every event on the "
                "trigger."
            ),
        )
        config: SubscriptionConfig = Field(
            ...,
            description=(
                "Action discriminated union: start_chat / chat_message / "
                "agent_fresh_session / graph_fresh_session."
            ),
        )
        reply_target: ReplyTarget | None = Field(
            default=None,
            description=(
                "Where the action's outbound reply goes; defaults to the "
                "source thread."
            ),
        )
        payload_template: str | None = None
        parallelism: str = "skip"
        description: str | None = Field(default=None, max_length=2000)
        enabled: bool = True

    def _binding_deps() -> ServiceDeps:
        return ServiceDeps(storage_provider=storage_provider)

    async def _create_channel_binding_handler(
        arguments: dict[str, Any],
    ) -> ToolCallResult:
        try:
            args = _CreateChannelBindingArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        try:
            sub = await create_subscription(
                trigger_id=args.trigger_id,
                config=args.config,
                event_matcher=args.event_matcher,
                reply_target=args.reply_target,
                payload_template=args.payload_template,
                parallelism=args.parallelism,
                description=args.description,
                enabled=args.enabled,
                deps=_binding_deps(),
            )
        except TriggerNotFound as exc:
            return _err(str(exc), error_type="trigger_not_found")
        return _ok(sub)

    registry["create_channel_binding"] = (
        make_tool(
            id="create_channel_binding",
            toolset_id=SYSTEM_TOOLSET_ID,
            purpose=(
                "Create an inbound channel binding (a matcher -> action "
                "subscription) on a channel trigger."
            ),
            when=(
                "Use when mapping a normalized channel event (message.posted "
                "/ command.invoked) to a platform action (start_chat / "
                "chat_message / agent_fresh_session / graph_fresh_session). "
                "The trigger must already exist as a channel-kind trigger; "
                "unknown trigger returns ``type=trigger_not_found``."
            ),
            args_schema=_CreateChannelBindingArgs.model_json_schema(),
            examples=[
                ToolExample(
                    args={
                        "trigger_id": "trg-ch-1",
                        "event_matcher": {
                            "event_type": "command.invoked",
                            "command_name": "deploy",
                        },
                        "config": {
                            "kind": "agent_fresh_session",
                            "workspace_id": "ws-1",
                            "agent_id": "deployer",
                        },
                        "reply_target": "source_thread",
                    },
                    returns="the created Subscription",
                    note="slash /deploy -> run the deployer agent, reply in-thread",
                ),
            ],
        ),
        _create_channel_binding_handler,
    )

    class _ListChannelBindingsArgs(BaseModel):
        trigger_id: str = Field(
            ..., min_length=1, description="Id of the channel trigger."
        )

    async def _list_channel_bindings_handler(
        arguments: dict[str, Any],
    ) -> ToolCallResult:
        try:
            args = _ListChannelBindingsArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        deps = _binding_deps()
        try:
            await get_trigger(trigger_id=args.trigger_id, deps=deps)
        except TriggerNotFound as exc:
            return _err(str(exc), error_type="trigger_not_found")
        items = await list_subscriptions(trigger_id=args.trigger_id, deps=deps)
        return _ok(items)

    registry["list_channel_bindings"] = (
        make_tool(
            id="list_channel_bindings",
            toolset_id=SYSTEM_TOOLSET_ID,
            purpose="List the inbound channel bindings on a channel trigger.",
            when=(
                "Use when you need every binding (matcher -> action "
                "subscription) for a given ``trigger_id``; returns an array "
                "of Subscription rows, or ``type=trigger_not_found`` if the "
                "trigger does not exist."
            ),
            args_schema=_ListChannelBindingsArgs.model_json_schema(),
            examples=[
                ToolExample(
                    args={"trigger_id": "trg-ch-1"},
                    returns="the channel bindings of trg-ch-1",
                ),
            ],
        ),
        _list_channel_bindings_handler,
    )

    class _DeleteChannelBindingArgs(BaseModel):
        trigger_id: str = Field(
            ..., min_length=1, description="Id of the channel trigger."
        )
        subscription_id: str = Field(
            ..., min_length=1, description="Id of the binding (Subscription) to delete."
        )

    async def _delete_channel_binding_handler(
        arguments: dict[str, Any],
    ) -> ToolCallResult:
        try:
            args = _DeleteChannelBindingArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        try:
            await delete_subscription(
                trigger_id=args.trigger_id,
                subscription_id=args.subscription_id,
                deps=_binding_deps(),
            )
        except SubscriptionNotFound as exc:
            return _err(str(exc), error_type="subscription_not_found")
        return _ok({"ok": True})

    registry["delete_channel_binding"] = (
        make_tool(
            id="delete_channel_binding",
            toolset_id=SYSTEM_TOOLSET_ID,
            purpose="Delete one inbound channel binding from a channel trigger.",
            when=(
                "Use when removing a single binding (Subscription) while "
                "keeping the trigger and its other bindings. Returns "
                "``{ok: true}`` or ``type=subscription_not_found``."
            ),
            args_schema=_DeleteChannelBindingArgs.model_json_schema(),
            examples=[
                ToolExample(
                    args={"trigger_id": "trg-ch-1", "subscription_id": "sb-1"},
                    returns="``{ok: true}``",
                ),
            ],
        ),
        _delete_channel_binding_handler,
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
    # Build a ToolApprovalPolicy resolver so call_tool's meta-dispatch
    # path enforces the same approval gate the agent loop applies; without
    # this a gated tool invoked via system__call_tool would run unguarded.
    approval_resolver = ApprovalResolver(
        storage=storage_provider.get_storage(ToolApprovalPolicy),
    )
    name, entry = _list_toolset_tools_tool(provider_registry)
    registry[name] = entry
    name, entry = _call_tool_tool(provider_registry, approval_resolver)
    registry[name] = entry

    # ---- Collection / Document extras --------------------------------
    registry.update(
        _collection_extras(
            storage_provider=storage_provider,
            provider_registry=provider_registry,
            semantic_search_registry=semantic_search_registry,
        )
    )
    registry.update(
        _document_extras(
            service_factory=_document_service_factory(
                storage_provider=storage_provider,
                provider_registry=provider_registry,
                semantic_search_registry=semantic_search_registry,
            )
        )
    )

    # ---- Dynamic invocation: invoke_agent ----------------------------
    class _InvokeAgentArgs(BaseModel):
        agent_id: str = Field(..., min_length=1, description="Agent to run.")
        prompt: str = Field(
            ..., min_length=1, description="Input for the subagent."
        )

    async def _invoke_agent_handler(
        arguments: dict[str, Any], *, ctx: ToolContext | None = None,
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
                    approval_resolver=approval_resolver,
                    session_id=getattr(ctx, "session_id", None),
                    workspace_id=getattr(ctx, "workspace_id", None),
                    chat_id=getattr(ctx, "chat_id", None),
                    invoke_tool_call_id=getattr(ctx, "tool_call_id", None),
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

    # ---- ask_user (yielding; available everywhere incl. chats) -------
    registry["ask_user"] = (
        make_tool(
            id="ask_user",
            toolset_id=toolset_id,
            purpose=(
                "Ask the human operator a question and pause the agent's "
                "turn until they type a reply; returns ``{response: "
                "<any>}`` (or ``{timed_out}`` / ``{cancelled}``)."
            ),
            when=(
                "Use when you genuinely need human input (clarification, "
                "approval, a choice the agent cannot make autonomously); "
                "not for status updates, and not for waiting a fixed "
                "duration (use ``sleep``)."
            ),
            args_schema=_AskUserArgs.model_json_schema(),
            examples=[
                ToolExample(
                    args={"prompt": "Proceed with deploy?"},
                    returns="operator's typed reply",
                    note="yielding; worker released",
                ),
            ],
            yields=True,
            requires_session=True,
        ),
        _ask_user_handler,
    )

    logger.info(
        "system toolset assembled with %d tools (id=%s)",
        len(registry),
        toolset_id,
    )

    return InternalToolsetProvider(toolset_id=toolset_id, registry=registry)


# Register the ask_user yielding-tool resume hook at import time. The
# worker's resume path looks up hooks by the BARE tool name from this
# central registry - the bare name is unchanged by the move from misc
# to system, so the key stays "ask_user".
from primer.worker.yield_resume_registry import register_resume_hook  # noqa: E402

register_resume_hook("ask_user", ask_user_resume)


__all__ = ["SYSTEM_TOOLSET_ID", "build_system_toolset"]
