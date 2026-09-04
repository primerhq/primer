"""REST router for ToolApprovalPolicy CRUD + invalidate."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field

from primer.api.deps import (
    get_approval_resolver,
    get_claim_engine,
    get_event_bus,
    get_provider_registry,
    get_session_storage,
    get_storage_provider,
    require_user,
)
from fastapi import HTTPException
from primer.api.errors import common_responses
from primer.api.routers._crud import make_crud_router
from primer.int.claim import ClaimEngine
from primer.int.event_bus import EventBus
from primer.model.except_ import ConflictError, NotFoundError
from primer.session.pending_gates import enumerate_pending_gates, resolve_pending_gate
from primer.session.yields import durably_wake_session
from primer.model.workspace_session import WorkspaceSession
from primer.model.storage import OffsetPage, OffsetPageResponse, OrderBy
from primer.storage.q import Q
from primer.model.tool_approval import (
    ApproverSpec,
    LlmApprovalConfig,
    PolicyApprovalConfig,
    ToolApprovalPolicy,
    ToolApprovalRecord,
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
        from primer.agent.rego import RegoCompileError, evaluate_policy
        try:
            evaluate_policy(cfg.policy, {})
        except RegoCompileError as exc:
            raise _validation_error(
                field_path="approval.policy",
                message=f"rego compile failed: {exc}",
            ) from exc
    elif isinstance(cfg, LlmApprovalConfig):
        from primer.model.provider import LLMProvider
        # Fetch the stored row directly via storage; the registry only
        # exposes the live adapter (get_llm), not the row.
        sp = provider_registry._sp  # noqa: SLF001
        row = await sp.get_storage(LLMProvider).get(cfg.provider_id)
        if row is None:
            raise _validation_error(
                field_path="approval.provider_id",
                message=f"unknown LLM provider {cfg.provider_id!r}",
            )
        # An LLM provider no longer carries a models[] list: what it
        # serves is its ModelProfile rows. The judge calls the adapter
        # with a bare model name (no agent, so no profile to resolve), so
        # the check stays "is this name published by that provider" -- it
        # just reads the profiles to answer it.
        names = await _published_model_names(sp, cfg.provider_id)
        if cfg.model not in names:
            raise _validation_error(
                field_path="approval.model",
                message=(
                    f"model {cfg.model!r} not registered on provider "
                    f"{cfg.provider_id!r} (available: {sorted(names)})"
                ),
            )


async def _published_model_names(sp, provider_id: str) -> set[str]:
    """Distinct model names the provider's ModelProfile rows name.

    Mirrors ``GET /v1/llm_providers/{id}/models``. Paged because a
    provider with many profiles is the expected shape once an operator
    has fetched a large upstream catalogue.
    """
    from primer.model.model_profile import ModelProfile
    from primer.model.storage import OffsetPage
    from primer.storage.q import Q

    names: set[str] = set()
    offset = 0
    store = sp.get_storage(ModelProfile)
    while True:
        page = await store.find(
            Q(ModelProfile).where("provider_id", provider_id).build(),
            OffsetPage(offset=offset, length=200),
        )
        names.update(p.model_name for p in page.items)
        if len(page.items) < 200:
            return names
        offset += 200


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
    """Response payload for GET .../tool_approval/pending.

    ``status`` is always ``"pending"`` from this endpoint: a parked
    session/chat is, by definition, still awaiting a decision. The field
    is part of the envelope so the Approvals records view can sort a
    unified records list by status. Resolved (``approved``/``rejected``)
    records are NOT persisted today, so they never surface here.
    """

    tool_call_id: str
    tool_name: str
    toolset_id: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    policy_id: str | None = None
    approval_type: str | None = None
    gate_reason: str | None = None
    approvers: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Who may decide (P6 approver routing): the resolved "
            "ApproverSpec stamped at park time. None means anyone."
        ),
    )
    parked_at: str
    timeout_at: str | None = None
    status: Literal["pending", "approved", "rejected"] = "pending"


class ToolApprovalRespondBody(BaseModel):
    """Request body for POST .../tool_approval/respond."""

    tool_call_id: str
    decision: Literal["approved", "rejected"]
    reason: str | None = Field(default=None, max_length=1024)


def _enforce_approvers(metadata: dict, user: Any) -> None:
    """403 ``approver_mismatch`` unless the caller may decide this gate.

    ``metadata`` is the SPECIFIC pending gate's ``resume_metadata``
    (from :func:`~primer.session.pending_gates.resolve_pending_gate`),
    not necessarily the session's primary/top-level one -- a graph park
    can have several pending approval gates at once, each with its own
    resolved spec, so enforcement must check the gate actually being
    decided rather than whichever one happens to be projected first.

    The spec was resolved and stamped at park time (policy row default,
    or the evaluator's per-call override). No spec means anyone.
    Admins always pass (see :class:`ApproverSpec.allows`); a malformed
    stored spec fails OPEN to anyone rather than wedging the park.
    """
    if user is None:  # WS scope / auth-disabled synthetic admin absent
        return
    raw = metadata.get("approvers")
    if not raw:
        return
    try:
        spec = ApproverSpec.model_validate(raw)
    except Exception:  # noqa: BLE001
        logger.warning("malformed stored approvers %r; allowing anyone", raw)
        return
    if not spec.allows(
        username=getattr(user, "username", ""),
        role=getattr(user, "role", ""),
    ):
        raise HTTPException(
            status_code=403,
            detail={"error": "approver_mismatch"},
        )


def _approval_blob_or_404(sess: Any, id_str: str) -> dict:
    """Return parked_state blob when the row has a pending _approval gate.

    Raises :class:`NotFoundError` if:
    * the row is None (doesn't exist),
    * it isn't in a parked/resumable state, or
    * none of its pending entries is an approval gate.

    Checks every pending entry via
    :func:`~primer.session.pending_gates.enumerate_pending_gates`, not
    just the top-level ``yielded`` projection: a graph park's outer
    ``yielded.tool_name`` is hardcoded ``"_approval"`` for ANY graph
    park regardless of what's actually primary (see
    ``_CheckpointMixin._build_pending_park_yield``), so the old
    top-level-only check could both false-positive (primary is really an
    ask_user agent yield) and false-negative (an _approval gate is
    pending but isn't the primary entry).
    """
    if sess is None:
        raise NotFoundError(f"{id_str!r} does not exist")
    if sess.parked_status not in ("parked", "resumable"):
        raise NotFoundError(f"{id_str!r} has no pending tool_approval")
    blob: dict = sess.parked_state or {}
    if not any(
        gate["kind"] == "_approval" for gate in enumerate_pending_gates(blob)
    ):
        raise NotFoundError(f"{id_str!r} is parked on a different tool")
    return blob


def _build_pending_response(
    blob: dict, sess: Any
) -> ToolApprovalPendingResponse:
    """Construct the pending-response envelope for ONE approval gate.

    This endpoint's response shape is singular (predates multi-gate
    graph parks), so a session with several pending approval gates at
    once surfaces only the first found here -- callers that need every
    pending gate use ``GET .../yields/pending`` (workspaces.py) instead,
    which returns all of them via the same shared resolver.
    """
    gate = next(
        (g for g in enumerate_pending_gates(blob) if g["kind"] == "_approval"),
        None,
    )
    metadata: dict = (gate or {}).get("resume_metadata") or {}
    original: dict = metadata.get("original_call") or {}
    # Graph pending-toolcall entries never carry a timeout (the checkpoint's
    # _PendingToolCall has no such field); this stays None for every graph
    # park, same as before this fix.
    yielded: dict = blob.get("yielded") or {}
    timeout = yielded.get("timeout")
    timeout_at_iso: str | None = None
    if timeout is not None and sess.parked_at is not None:
        timeout_at_iso = (
            sess.parked_at + timedelta(seconds=float(timeout))
        ).isoformat()
    return ToolApprovalPendingResponse(
        tool_call_id=original.get("id") or (gate or {}).get("tool_call_id") or "",
        tool_name=original.get("name", ""),
        arguments=original.get("arguments") or {},
        policy_id=metadata.get("policy_id"),
        approval_type=metadata.get("approval_type"),
        gate_reason=metadata.get("gate_reason"),
        approvers=metadata.get("approvers"),
        parked_at=(
            sess.parked_at.isoformat()
            if sess.parked_at is not None
            else ""
        ),
        timeout_at=timeout_at_iso,
    )


async def _publish_decision(
    *,
    sess: Any,
    id_str: str,
    body: ToolApprovalRespondBody,
    gate: dict[str, Any],
    event_bus: EventBus,
    session_storage,
    engine: ClaimEngine | None,
    storage_provider=None,
    decided_by: str | None = None,
) -> bool:
    """Durably flip the row on ``gate``'s own event_key, then wake the bus.

    ``gate`` is the SPECIFIC pending entry ``body.tool_call_id`` resolved
    to (see :func:`~primer.session.pending_gates.resolve_pending_gate`),
    not necessarily the session's primary one -- a graph park can have
    several pending approval gates open at once, each waking on its own
    event_key, so publishing the top-level/primary key here would answer
    the wrong gate (or 404) for every non-primary one.

    D-C2 fix: the operator decision is stamped onto the session row
    (``resume_event_payload`` + ``parked_status='resumable'`` + the claim
    lease re-armed) BEFORE the bus publish, so a decision is never lost when
    the bus listener is down/reconnecting - LISTEN/NOTIFY is not durable but
    the row is, and the claim loop admits ``resumable`` rows without any bus.
    The publish that follows is a best-effort immediate wake only.

    Uses :func:`durably_wake_session`, which acts on the flip helper's bool:
    a guard-rejected row that is already ``resumable`` gets its claim lease
    re-armed, so a retry after a half-applied flip (row stamped, lease lost)
    repairs the row rather than accepting a decision the claim loop can never
    act on. Returns True when this call advanced the row.
    """
    event_key: str | None = gate.get("event_key")
    if not event_key:
        raise NotFoundError(f"{id_str!r} park is missing event_key")
    payload = {
        "decision": body.decision,
        "reason": body.reason,
        "decided_by": decided_by,
    }
    did = await durably_wake_session(
        sess,
        event_key=event_key,
        payload=payload,
        session_storage=session_storage,
        engine=engine,
    )
    if storage_provider is not None:
        # 01a068da: write the durable ToolApprovalRecord HERE, at the
        # moment the decision actually arrives, rather than waiting for
        # the resume coordinator to get around to it (session_resume_
        # coordinator.py's write_approval_record_for_session, which used
        # to be the ONLY write site - a crash between this respond and
        # that eventual resume lost the audit record entirely). Attempted
        # unconditionally, not gated on `did`: a retry after a half-
        # applied first attempt (row already resumable, but that earlier
        # call's record write itself failed for some unrelated reason)
        # still gets a fresh try, and gate_event_key's unique index makes
        # a genuine duplicate attempt a safe no-op either way (see
        # write_approval_record's own docstring). classify_approval_
        # payload is the SAME classifier the resume path uses on this
        # SAME payload shape, so the record's verdict cannot drift from
        # whatever the eventual resume computes.
        from primer.agent.approval_record import (
            record_from_parked_blob,
            write_approval_record,
        )
        from primer.model.tool_approval import ToolApprovalRecord
        from primer.worker.yield_runtime import classify_approval_payload

        decision, reason = classify_approval_payload(payload)
        # record_from_parked_blob reads a ``{"yielded": {"resume_metadata":
        # ...}}``-shaped blob; project the resolved gate into that shape
        # rather than passing the session's raw parked_state, which would
        # describe the PRIMARY entry, not necessarily this one.
        record = record_from_parked_blob(
            blob={
                "tool_call_id": gate.get("tool_call_id"),
                "yielded": {"resume_metadata": gate.get("resume_metadata") or {}},
            },
            decision=decision,
            reason=reason,
            agent_id=getattr(sess.binding, "agent_id", None),
            session_id=id_str,
            requested_at=sess.parked_at,
            decided_by=decided_by,
            gate_event_key=event_key,
        )
        await write_approval_record(
            storage_provider.get_storage(ToolApprovalRecord), record,
        )

        from primer.events.wake import emit_session_wake

        await emit_session_wake(storage_provider, event_bus, event_key, payload)
        return did
    try:
        await event_bus.publish(event_key, payload)
    except Exception:  # noqa: BLE001
        logger.exception(
            "tool_approval decision publish failed for event_key=%r; durable "
            "flip already persisted, claim loop will recover", event_key,
        )
    return did


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


def make_tool_approval_ops_router() -> APIRouter:
    """Pending/respond/records: the OPERATOR surface (P6 gating split).

    Deciding a gated call is ordinary operator work, so this router
    mounts at the user tier; who may decide a SPECIFIC call is the
    approver spec's business, enforced per park by
    :func:`_enforce_approvers`. Policy CRUD (the factory above) stays
    admin - configuring the gates is system policy.
    """
    router = APIRouter(tags=[_TAG])

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
        request: Request,
        session_storage=Depends(get_session_storage),
        event_bus: EventBus = Depends(get_event_bus),
        engine: ClaimEngine | None = Depends(get_claim_engine),
        user=Depends(require_user),
    ) -> dict[str, str]:
        sess = await session_storage.get(session_id)
        blob = _approval_blob_or_404(sess, session_id)
        # Resolve the SPECIFIC gate body.tool_call_id names -- a graph park
        # can have several pending approval gates open at once, and only
        # the primary one is projected onto the top-level `yielded` blob
        # (see primer.session.pending_gates). Mirrors yields.py's
        # _graph_ask_user_dispatch pattern for the ask_user case.
        gate = resolve_pending_gate(blob, tool_call_id=body.tool_call_id, kind="_approval")
        if gate is None:
            raise NotFoundError(
                f"No pending tool_approval with tool_call_id "
                f"{body.tool_call_id!r} on {session_id!r}"
            )
        # Approver routing (P6): 403 approver_mismatch before any state
        # moves; decided_by rides the wake payload into the durable
        # record the resume coordinator writes.
        _enforce_approvers(gate.get("resume_metadata") or {}, user)
        await _publish_decision(
            sess=sess,
            id_str=session_id,
            body=body,
            gate=gate,
            event_bus=event_bus,
            session_storage=session_storage,
            engine=engine,
            storage_provider=get_storage_provider(request),
            decided_by=getattr(user, "username", None),
        )
        from primer.events.recorder import actor_of, recorder_for

        sp = get_storage_provider(request)
        await recorder_for(sp, event_bus).emit(
            "approval.decided",
            actor=actor_of(getattr(request.state, "actor", None)),
            session_id=session_id,
            payload={
                "decision": body.decision,
                "tool_call_id": body.tool_call_id,
            },
        )
        return {"status": "accepted"}

    # -----------------------------------------------------------------------
    # Resolved approval records (durable history)
    # -----------------------------------------------------------------------

    @router.get(
        "/tool_approval/records",
        response_model=OffsetPageResponse[ToolApprovalRecord],
        responses=common_responses(422, 500),
    )
    async def list_tool_approval_records(
        request: Request,
        status: Annotated[
            Literal["all", "approved", "rejected", "timeout", "cancelled"],
            Query(),
        ] = "all",
        session_id: Annotated[
            str | None,
            Query(
                description=(
                    "Scope to one session's resolved decisions - the "
                    "session detail transcript's resolved-card renderer "
                    "needs exactly this session's history, not a page of "
                    "the whole instance's records to filter client-side."
                ),
            ),
        ] = None,
        gate_event_key: Annotated[
            str | None,
            Query(
                description=(
                    "Look up the record for one specific gate "
                    "(ParkedState.yielded.event_key). 01a068da: the field "
                    "carries a unique index, so this narrows to at most "
                    "one record - useful for a caller that has the event "
                    "key in hand (e.g. confirming a just-submitted "
                    "decision landed) and does not want to page through "
                    "session_id history to find it."
                ),
            ),
        ] = None,
        offset: Annotated[int, Query(ge=0)] = 0,
        length: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> OffsetPageResponse[ToolApprovalRecord]:
        """List resolved approval decisions, newest first.

        ``status`` filters by decision (``all`` = no filter). ``session_id``
        optionally scopes to one session; ``gate_event_key`` optionally
        narrows to one gate. Ordered by ``decided_at`` descending so the
        most recent decisions lead, mirroring the records view's default
        sort.
        """
        sp = get_storage_provider(request)
        storage = sp.get_storage(ToolApprovalRecord)
        page = OffsetPage(offset=offset, length=length)
        order = [OrderBy(field="decided_at", direction="desc")]
        if status == "all" and session_id is None and gate_event_key is None:
            return await storage.list(page, order_by=order)
        query = Q(ToolApprovalRecord)
        if session_id is not None:
            query = query.where("session_id", session_id)
        if gate_event_key is not None:
            query = query.where("gate_event_key", gate_event_key)
        if status != "all":
            query = query.where("decision", status)
        return await storage.find(query.build(), page, order_by=order)

    return router


__all__ = ["make_tool_approval_ops_router", "make_tool_approval_router"]
