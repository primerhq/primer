"""REST router for ToolApprovalPolicy CRUD + invalidate."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError

from matrix.api.deps import (
    get_approval_resolver,
    get_provider_registry,
    get_storage_provider,
)
from matrix.api.routers._crud import make_crud_router
from matrix.model.except_ import ConflictError
from matrix.model.storage import FieldRef, OffsetPage, Op, Predicate, Value
from matrix.model.tool_approval import (
    LlmApprovalConfig,
    PolicyApprovalConfig,
    ToolApprovalPolicy,
)


logger = logging.getLogger(__name__)


_PLURAL = "tool_approval_policies"
_TAG = "tool_approval_policies"


def _get_tool_approval_policy_storage(request: Request):
    """Storage dependency for ToolApprovalPolicy."""
    sp = get_storage_provider(request)
    return sp.get_storage(ToolApprovalPolicy)


async def _validate_uniqueness(
    entity: ToolApprovalPolicy,
    *,
    storage,
    skip_id: str | None = None,
) -> None:
    predicate = Predicate(
        left=Predicate(
            left=FieldRef(name="toolset_id"),
            op=Op.EQ,
            right=Value(value=entity.toolset_id),
        ),
        op=Op.AND,
        right=Predicate(
            left=FieldRef(name="tool_name"),
            op=Op.EQ,
            right=Value(value=entity.tool_name),
        ),
    )
    page = await storage.find(predicate, OffsetPage(offset=0, length=10))
    for existing in page.items:
        if skip_id is not None and existing.id == skip_id:
            continue
        raise ConflictError(
            f"a ToolApprovalPolicy for "
            f"toolset_id={entity.toolset_id!r}, "
            f"tool_name={entity.tool_name!r} already exists "
            f"(id={existing.id!r})"
        )


async def _validate_approval_config(
    entity: ToolApprovalPolicy,
    *,
    provider_registry,
) -> None:
    cfg = entity.approval
    if isinstance(cfg, PolicyApprovalConfig):
        from matrix.agent.rego import RegoCompileError, evaluate_policy
        try:
            evaluate_policy(cfg.policy, {})
        except RegoCompileError as exc:
            raise _validation_error(
                field_path="approval.policy",
                message=f"rego compile failed: {exc}",
            ) from exc
    elif isinstance(cfg, LlmApprovalConfig):
        from matrix.model.provider import LLMProvider
        # Fetch the stored row directly via storage; the registry only
        # exposes the live adapter (get_llm), not the row.
        sp = provider_registry._sp  # noqa: SLF001
        row = await sp.get_storage(LLMProvider).get(cfg.provider_id)
        if row is None:
            raise _validation_error(
                field_path="approval.provider_id",
                message=f"unknown LLM provider {cfg.provider_id!r}",
            )
        names = {m.name for m in row.models}
        if cfg.model not in names:
            raise _validation_error(
                field_path="approval.model",
                message=(
                    f"model {cfg.model!r} not registered on provider "
                    f"{cfg.provider_id!r} (available: {sorted(names)})"
                ),
            )


def _validation_error(*, field_path: str, message: str) -> RequestValidationError:
    return RequestValidationError(
        errors=[
            {
                "loc": tuple(field_path.split(".")),
                "msg": message,
                "type": "value_error",
            }
        ],
    )


def make_tool_approval_router() -> APIRouter:
    router = APIRouter(tags=[_TAG])

    async def on_pre_create(entity: ToolApprovalPolicy, request: Request) -> None:
        storage_provider = get_storage_provider(request)
        provider_registry = get_provider_registry(request)
        storage = storage_provider.get_storage(ToolApprovalPolicy)
        await _validate_uniqueness(entity, storage=storage)
        await _validate_approval_config(
            entity, provider_registry=provider_registry,
        )

    async def on_pre_update(
        entity: ToolApprovalPolicy,
        existing: ToolApprovalPolicy,
        request: Request,
    ) -> None:
        storage_provider = get_storage_provider(request)
        provider_registry = get_provider_registry(request)
        storage = storage_provider.get_storage(ToolApprovalPolicy)
        await _validate_uniqueness(entity, storage=storage, skip_id=existing.id)
        await _validate_approval_config(
            entity, provider_registry=provider_registry,
        )

    crud = make_crud_router(
        model_cls=ToolApprovalPolicy,
        storage_dep=_get_tool_approval_policy_storage,
        plural=_PLURAL,
        tag=_TAG,
        on_pre_create=on_pre_create,
        on_pre_update=on_pre_update,
    )
    router.include_router(crud)

    @router.post(f"/{_PLURAL}/invalidate", status_code=202)
    async def invalidate(
        approval_resolver=Depends(get_approval_resolver),
    ) -> dict[str, str]:
        approval_resolver.invalidate()
        return {"status": "accepted"}

    return router


__all__ = ["make_tool_approval_router"]
