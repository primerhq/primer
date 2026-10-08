"""Pre-write checks for :class:`~primer.model.tool_approval.ToolApprovalPolicy`, shared by the REST router and the system tools
(task 01a111d1, D5 phase 2b).

Moved out of ``primer/api/routers/tool_approval.py``, where they were bound to a ``Request``: one function per check over
``(entity, storage_provider)`` raising :class:`~primer.common.entity_checks.EntityCheckError`. The router's hooks re-raise the exact
exceptions they always raised (``ConflictError`` for a clash, a ``RequestValidationError`` with a ``body.*`` loc for a bad config),
the system ``create_`` / ``update_tool_approval_policy`` tools answer ``conflict`` / ``validation-error``.

The order is the router's: uniqueness first, then the approval config.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from primer.common.entity_checks import EntityCheckError
from primer.common.preview_paths import missing_paths
from primer.model.model_profile import ModelProfile
from primer.model.provider import LLMProvider
from primer.model.storage import OffsetPage
from primer.model.tool_approval import LlmApprovalConfig, PolicyApprovalConfig, ToolApprovalPolicy
from primer.storage.q import Q

if TYPE_CHECKING:
    from primer.int.storage_provider import StorageProvider

# ``(toolset_id, tool_name) -> the tool's args_schema``, or None when the tool cannot be found (primer/agent/tool_schemas.py ``find_tool_schema``).
ToolSchemaOf = Callable[[str, str], Awaitable["dict[str, Any] | None"]]


async def check_policy_unique(
    entity: ToolApprovalPolicy, *, storage_provider: "StorageProvider", skip_id: str | None = None,
) -> None:
    """At most one policy per ``(toolset_id, tool_name)``; ``skip_id`` is the row being updated (it may match itself)."""
    storage = storage_provider.get_storage(ToolApprovalPolicy)
    predicate = (
        Q(ToolApprovalPolicy)
        .where("toolset_id", entity.toolset_id)
        .where("tool_name", entity.tool_name)
        .build()
    )
    page = await storage.find(predicate, OffsetPage(offset=0, length=10))
    for existing in page.items:
        if skip_id is not None and existing.id == skip_id:
            continue
        raise EntityCheckError(
            "conflict",
            f"a ToolApprovalPolicy for "
            f"toolset_id={entity.toolset_id!r}, "
            f"tool_name={entity.tool_name!r} already exists "
            f"(id={existing.id!r})",
        )


async def check_approval_config(entity: ToolApprovalPolicy, *, storage_provider: "StorageProvider") -> None:
    """A Rego policy must compile; an LLM judge must name a stored provider and a model that provider publishes."""
    cfg = entity.approval
    if isinstance(cfg, PolicyApprovalConfig):
        from primer.agent.rego import RegoCompileError, evaluate_policy

        try:
            evaluate_policy(cfg.policy, {})
        except RegoCompileError as exc:
            raise EntityCheckError("validation", f"rego compile failed: {exc}", field="approval.policy") from exc
    elif isinstance(cfg, LlmApprovalConfig):
        # Fetch the stored row directly via storage; the registry only exposes the live adapter (get_llm), not the row.
        row = await storage_provider.get_storage(LLMProvider).get(cfg.provider_id)
        if row is None:
            raise EntityCheckError(
                "validation", f"unknown LLM provider {cfg.provider_id!r}", field="approval.provider_id",
            )
        # An LLM provider no longer carries a models[] list: what it serves is its ModelProfile rows. The judge calls the adapter
        # with a bare model name (no agent, so no profile to resolve), so the check stays "is this name published by that
        # provider" -- it just reads the profiles to answer it.
        names = await published_model_names(storage_provider, cfg.provider_id)
        if cfg.model not in names:
            raise EntityCheckError(
                "validation",
                f"model {cfg.model!r} not registered on provider "
                f"{cfg.provider_id!r} (available: {sorted(names)})",
                field="approval.model",
            )


async def published_model_names(storage_provider: "StorageProvider", provider_id: str) -> set[str]:
    """Distinct model names the provider's ModelProfile rows name.

    Mirrors ``GET /v1/llm_providers/{id}/models``. Paged because a provider with many profiles is the expected shape once an
    operator has fetched a large upstream catalogue.
    """
    names: set[str] = set()
    offset = 0
    store = storage_provider.get_storage(ModelProfile)
    while True:
        page = await store.find(
            Q(ModelProfile).where("provider_id", provider_id).build(),
            OffsetPage(offset=offset, length=200),
        )
        names.update(p.model_name for p in page.items)
        if len(page.items) < 200:
            return names
        offset += 200


async def check_preview_args(entity: ToolApprovalPolicy, *, tool_schema_of: "ToolSchemaOf | None") -> None:
    """Every path of ``preview_args`` must name an argument of the tool the policy gates (design note 01a11cd3-66b0). A path that names nothing is a typo, and a typo
    hides more than the operator meant, silently. A policy with no paths (``None`` or ``[]``) is never checked, so a policy on a tool that is not in the catalogue
    right now stays writable. Paths with a tool that cannot be found cannot be checked: the operator is told so rather than trusted."""
    paths = entity.preview_args
    if not paths:
        return
    if tool_schema_of is None:
        raise EntityCheckError("validation", "preview_args cannot be checked here: no way to find the tool's arguments was given", field="preview_args")
    schema = await tool_schema_of(entity.toolset_id, entity.tool_name)
    if schema is None:
        raise EntityCheckError(
            "validation",
            f"tool {entity.tool_name!r} of toolset {entity.toolset_id!r} is not in the catalogue right now, so preview_args cannot be checked against its arguments; "
            "set preview_args once the tool is reachable",
            field="preview_args",
        )
    gone = missing_paths(schema, paths)
    if gone:
        names = sorted(schema.get("properties") or {})
        raise EntityCheckError(
            "validation",
            f"preview_args {gone} name no argument of the tool {entity.tool_name!r} of toolset {entity.toolset_id!r} (top-level arguments: {names})",
            field="preview_args",
        )


async def check_policy(
    entity: ToolApprovalPolicy, *, storage_provider: "StorageProvider", skip_id: str | None = None, tool_schema_of: "ToolSchemaOf | None" = None,
) -> None:
    """Every pre-write check for a policy, in the router's order (``skip_id`` is the row being updated). ``tool_schema_of`` finds the gated tool's schema for the
    ``preview_args`` check; a caller whose policies name no path may omit it."""
    await check_policy_unique(entity, storage_provider=storage_provider, skip_id=skip_id)
    await check_approval_config(entity, storage_provider=storage_provider)
    await check_preview_args(entity, tool_schema_of=tool_schema_of)


__all__ = ["ToolSchemaOf", "check_approval_config", "check_policy", "check_policy_unique", "check_preview_args", "published_model_names"]
