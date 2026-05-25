"""REST router for ToolApprovalPolicy CRUD + invalidate."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, Path, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field

from matrix.api.deps import (
    get_approval_resolver,
    get_chat_storage,
    get_event_bus,
    get_provider_registry,
    get_session_storage,
    get_storage_provider,
)
from matrix.api.errors import common_responses
from matrix.api.routers._crud import make_crud_router
from matrix.int.event_bus import EventBus
from matrix.model.chats import Chat
from matrix.model.except_ import ConflictError, NotFoundError
from matrix.model.session import Session
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
    # Prepend "body" to the loc to match FastAPI/Pydantic's standard
    # body-field-error convention. The UI's modal lookups (approvals.jsx
    # fieldErr("body.approval.policy") etc.) expect this prefix; without
    # it the inline error renders as an empty string while the toast
    # path also stays silent.
    return RequestValidationError(
        errors=[
            {
                "loc": ("body",) + tuple(field_path.split(".")),
                "msg": message,
                "type": "value_error",
            }
        ],
    )


# ===========================================================================
# Tool-approval pending/respond models (§2 Task 8)
# ===========================================================================


class ToolApprovalPendingResponse(BaseModel):
    """Response payload for GET .../tool_approval/pending."""

    tool_call_id: str
    tool_name: str
    toolset_id: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    policy_id: str | None = None
    approval_type: str | None = None
    gate_reason: str | None = None
    parked_at: str
    timeout_at: str | None = None


class ToolApprovalRespondBody(BaseModel):
    """Request body for POST .../tool_approval/respond."""

    tool_call_id: str
    decision: Literal["approved", "rejected"]
    reason: str | None = Field(default=None, max_length=1024)


def _approval_blob_or_404(sess_or_chat: Any, id_str: str) -> dict:
    """Return parked_state blob when the row is parked on _approval.

    Raises :class:`NotFoundError` if:
    * the row is None (doesn't exist),
    * it isn't in a parked/resumable state, or
    * it's parked on a different tool.
    """
    if sess_or_chat is None:
        raise NotFoundError(f"{id_str!r} does not exist")
    if sess_or_chat.parked_status not in ("parked", "resumable"):
        raise NotFoundError(f"{id_str!r} has no pending tool_approval")
    blob: dict = sess_or_chat.parked_state or {}
    yielded: dict = blob.get("yielded") or {}
    if yielded.get("tool_name") != "_approval":
        raise NotFoundError(f"{id_str!r} is parked on a different tool")
    return blob


def _build_pending_response(
    blob: dict, sess_or_chat: Any
) -> ToolApprovalPendingResponse:
    """Construct the pending-response envelope from parked_state."""
    yielded: dict = blob.get("yielded") or {}
    metadata: dict = yielded.get("resume_metadata") or {}
    original: dict = metadata.get("original_call") or {}
    timeout = yielded.get("timeout")
    timeout_at_iso: str | None = None
    if timeout is not None and sess_or_chat.parked_at is not None:
        timeout_at_iso = (
            sess_or_chat.parked_at + timedelta(seconds=float(timeout))
        ).isoformat()
    return ToolApprovalPendingResponse(
        tool_call_id=original.get("id") or blob.get("tool_call_id", ""),
        tool_name=original.get("name", ""),
        arguments=original.get("arguments") or {},
        policy_id=metadata.get("policy_id"),
        approval_type=metadata.get("approval_type"),
        gate_reason=metadata.get("gate_reason"),
        parked_at=(
            sess_or_chat.parked_at.isoformat()
            if sess_or_chat.parked_at is not None
            else ""
        ),
        timeout_at=timeout_at_iso,
    )


async def _publish_decision(
    *,
    sess_or_chat: Any,
    id_str: str,
    body: ToolApprovalRespondBody,
    event_bus: EventBus,
) -> None:
    """Validate and publish the operator decision onto the event bus."""
    blob = _approval_blob_or_404(sess_or_chat, id_str)
    yielded: dict = blob.get("yielded") or {}
    original: dict = (yielded.get("resume_metadata") or {}).get("original_call") or {}
    expected = original.get("id") or blob.get("tool_call_id")
    if expected != body.tool_call_id:
        raise NotFoundError(
            f"No pending tool_approval with tool_call_id "
            f"{body.tool_call_id!r} on {id_str!r}"
        )
    event_key: str | None = yielded.get("event_key")
    if not event_key:
        raise NotFoundError(f"{id_str!r} park is missing event_key")
    await event_bus.publish(
        event_key,
        {"decision": body.decision, "reason": body.reason},
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

    # -----------------------------------------------------------------------
    # Tool-approval pending/respond for sessions (§2 Task 8)
    # -----------------------------------------------------------------------

    @router.get(
        "/sessions/{session_id}/tool_approval/pending",
        response_model=ToolApprovalPendingResponse,
        responses=common_responses(404, 500),
    )
    async def get_session_tool_approval_pending(
        session_id: Annotated[str, Path()],
        session_storage=Depends(get_session_storage),
    ) -> ToolApprovalPendingResponse:
        sess = await session_storage.get(session_id)
        blob = _approval_blob_or_404(sess, session_id)
        return _build_pending_response(blob, sess)

    @router.post(
        "/sessions/{session_id}/tool_approval/respond",
        status_code=202,
        responses=common_responses(404, 422, 500),
    )
    async def post_session_tool_approval_respond(
        session_id: Annotated[str, Path()],
        body: Annotated[ToolApprovalRespondBody, Body()],
        session_storage=Depends(get_session_storage),
        event_bus: EventBus = Depends(get_event_bus),
    ) -> dict[str, str]:
        sess = await session_storage.get(session_id)
        await _publish_decision(
            sess_or_chat=sess,
            id_str=session_id,
            body=body,
            event_bus=event_bus,
        )
        return {"status": "accepted"}

    # -----------------------------------------------------------------------
    # Tool-approval pending/respond for chats (§2 Task 8)
    # -----------------------------------------------------------------------

    @router.get(
        "/chats/{chat_id}/tool_approval/pending",
        response_model=ToolApprovalPendingResponse,
        responses=common_responses(404, 500),
    )
    async def get_chat_tool_approval_pending(
        chat_id: Annotated[str, Path()],
        chat_storage=Depends(get_chat_storage),
    ) -> ToolApprovalPendingResponse:
        chat = await chat_storage.get(chat_id)
        blob = _approval_blob_or_404(chat, chat_id)
        return _build_pending_response(blob, chat)

    @router.post(
        "/chats/{chat_id}/tool_approval/respond",
        status_code=202,
        responses=common_responses(404, 422, 500),
    )
    async def post_chat_tool_approval_respond(
        chat_id: Annotated[str, Path()],
        body: Annotated[ToolApprovalRespondBody, Body()],
        chat_storage=Depends(get_chat_storage),
        event_bus: EventBus = Depends(get_event_bus),
    ) -> dict[str, str]:
        chat = await chat_storage.get(chat_id)
        await _publish_decision(
            sess_or_chat=chat,
            id_str=chat_id,
            body=body,
            event_bus=event_bus,
        )
        return {"status": "accepted"}

    return router


__all__ = ["make_tool_approval_router"]
