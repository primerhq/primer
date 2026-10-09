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
from primer.agent.approval_checks import check_approval_config, check_policy_unique, check_preview_args
from primer.agent.tool_schemas import find_tool_schema
from primer.common.entity_checks import EntityCheckError
from primer.int.event_bus import EventBus
from primer.model.except_ import ConflictError, NotFoundError
from primer.api.approver_guard import enforce_approvers
from primer.api.gate_fence import count_gate_token, stale_gate_error
from primer.session.pending_gates import enumerate_pending_gates, resolve_pending_gate
from primer.model.yield_ import GATE_ID_PATTERN, gate_id_of
from primer.session.yields import durably_wake_session
from primer.model.workspace_session import WorkspaceSession
from primer.model.storage import OffsetPage, OffsetPageResponse, OrderBy
from primer.storage.q import Q
from primer.model.tool_approval import (
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


def _as_rest_error(exc: EntityCheckError) -> Exception:
    """The exception this router has always raised for the check: a ``ConflictError`` for a clash, a ``RequestValidationError``
    with a ``body.<field>`` loc for a refused config (the console's modal reads that loc)."""
    if exc.kind == "conflict":
        return ConflictError(exc.message)
    return _validation_error(field_path=exc.field or "", message=exc.message)


async def _validate_uniqueness(
    entity: ToolApprovalPolicy,
    *,
    storage_provider,
    skip_id: str | None = None,
) -> None:
    # The check itself is shared with the system tools: primer/agent/approval_checks.py.
    try:
        await check_policy_unique(entity, storage_provider=storage_provider, skip_id=skip_id)
    except EntityCheckError as exc:
        raise _as_rest_error(exc) from exc


async def _validate_approval_config(
    entity: ToolApprovalPolicy,
    *,
    storage_provider,
) -> None:
    try:
        await check_approval_config(entity, storage_provider=storage_provider)
    except EntityCheckError as exc:
        raise _as_rest_error(exc) from exc


async def _validate_preview_args(entity: ToolApprovalPolicy, *, provider_registry) -> None:
    """Every ``preview_args`` path must name an argument of the gated tool (shared check, primer/agent/approval_checks.py)."""

    async def schema_of(toolset_id: str, tool_name: str):
        return await find_tool_schema(provider_registry, toolset_id, tool_name)

    try:
        await check_preview_args(entity, tool_schema_of=schema_of)
    except EntityCheckError as exc:
        raise _as_rest_error(exc) from exc


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
    gate_id: str | None = Field(
        default=None,
        description=(
            "The id of THIS gate, minted when it was created. Send it back as ``gate_id`` on respond: the provider's tool_call_id repeats "
            "across rounds, so a respond that names only the tool_call_id can decide a LATER gate than the one the operator was looking at "
            "(409 ``approval_stale`` when it names a gate that has since been replaced). None for a park from before gates had ids."
        ),
    )
    parked_at: str
    timeout_at: str | None = None
    status: Literal["pending", "approved", "rejected"] = "pending"


class ToolApprovalRespondBody(BaseModel):
    """Request body for POST .../tool_approval/respond."""

    tool_call_id: str
    gate_id: str | None = Field(
        default=None,
        pattern=GATE_ID_PATTERN,
        description=(
            "The ``gate_id`` the pending response served for the gate this decision answers. Optional while clients catch up (a respond "
            "without it is accepted, logged and counted); a respond naming a gate that is no longer the pending one is a 409 "
            "``approval_stale`` and moves nothing."
        ),
    )
    decision: Literal["approved", "rejected"]
    reason: str | None = Field(default=None, max_length=1024)


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
        gate_id=gate_id_of(metadata),
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

        decision, reason, _kind = classify_approval_payload(payload)
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
        await _validate_uniqueness(entity, storage_provider=storage_provider)
        # The config check reads provider rows through the registry's own storage provider, as it always did.
        await _validate_approval_config(entity, storage_provider=provider_registry._sp)  # noqa: SLF001
        await _validate_preview_args(entity, provider_registry=provider_registry)

    async def on_pre_update(
        entity: ToolApprovalPolicy,
        existing: ToolApprovalPolicy,
        request: Request,
    ) -> None:
        storage_provider = get_storage_provider(request)
        provider_registry = get_provider_registry(request)
        await _validate_uniqueness(entity, storage_provider=storage_provider, skip_id=existing.id)
        await _validate_approval_config(entity, storage_provider=provider_registry._sp)  # noqa: SLF001
        await _validate_preview_args(entity, provider_registry=provider_registry)

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
    :func:`~primer.api.approver_guard.enforce_approvers`. Policy CRUD (the factory above) stays
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
        responses=common_responses(404, 409, 422, 500),
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
        gate = resolve_pending_gate(
            blob, tool_call_id=body.tool_call_id, kind="_approval", gate_id=body.gate_id,
        )
        if gate is None:
            # C-033: the call id is pending but not under the gate the card named => the card is stale (the gate it showed was replaced by
            # a later one under the same provider id). Refused before anything moves.
            if body.gate_id is not None and resolve_pending_gate(
                blob, tool_call_id=body.tool_call_id, kind="_approval",
            ) is not None:
                count_gate_token(kind="approval", session_id=session_id, token=body.gate_id, stale=True)
                raise stale_gate_error("approval")
            raise NotFoundError(
                f"No pending tool_approval with tool_call_id "
                f"{body.tool_call_id!r} on {session_id!r}"
            )
        count_gate_token(kind="approval", session_id=session_id, token=body.gate_id)
        # Approver routing (P6): 403 approver_mismatch before any state
        # moves; decided_by rides the wake payload into the durable
        # record the resume coordinator writes.
        enforce_approvers(gate.get("resume_metadata") or {}, user)
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
