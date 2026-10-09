"""REST surface for yielding-tool interactions (M3+).

Three endpoints, all rooted at ``/v1/sessions/{session_id}``:

* ``GET .../ask_user/pending`` — returns the operator-facing prompt
  payload when the session is parked on the ``ask_user`` tool. 404
  for any other state (no park, sleep park, etc.) — the panel only
  renders when the row is showing one.
* ``POST .../ask_user/respond`` — the operator's reply. Validates
  against the optional JSON Schema the tool supplied, publishes the
  reply on the event bus, returns 202 once queued.
* ``POST .../yields/{tool_call_id}/cancel`` — tool-agnostic cancel
  for a single in-flight yield. Publishes a ``YieldCancelled`` marker
  payload; the resume hook synthesises the cancelled tool result so
  the agent's turn keeps going.

The router consults the **event bus** for publish + the **session
storage** for the parked-state lookup. The bus listener (started in
the app lifespan) flips the parked row to resumable; the worker pool
picks it up via its claim loop. We do not call the scheduler
directly here — keeping that boundary clean means the same router
works against both the in-memory and the Postgres scheduler with no
code changes.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Path
from pydantic import BaseModel, Field

from primer.api.approver_guard import enforce_approvers
from primer.api.gate_fence import count_gate_token, stale_gate_error
from primer.api.deps import (
    get_claim_engine,
    get_event_bus,
    get_external_tool_call_storage,
    get_session_storage,
    get_storage_provider,
    require_user,
)
from primer.api.errors import common_responses
from primer.int.claim import ClaimEngine
from primer.int.event_bus import EventBus
from primer.model.except_ import (
    ConflictError,
    NotFoundError,
    ValidationError,
)
from primer.model.workspace_session import WorkspaceSession
from primer.model.yield_ import GATE_ID_PATTERN, gate_id_of, with_wake_gate
from primer.session.approvers import ADMIN_ONLY_METADATA
from primer.session.pending_gates import enumerate_pending_gates, resolve_pending_gate
from primer.session.yields import durably_wake_session
from primer.worker.yield_runtime import make_cancelled_payload


logger = logging.getLogger(__name__)

yields_router = APIRouter(tags=["yields"])


# ===========================================================================
# Shared lookups
# ===========================================================================


async def _load_session_or_404(session_storage, session_id: str) -> WorkspaceSession:
    sess = await session_storage.get(session_id)
    if sess is None:
        raise NotFoundError(f"Session {session_id!r} does not exist")
    return sess


def _parked_blob(sess: WorkspaceSession) -> dict[str, Any] | None:
    """Return the parked_state blob if the session is parked or
    resumable, else None.

    Both states qualify: a resumable row is one whose event already
    fired but the worker hasn't claimed it yet. Either way the
    in-flight tool_call_id is the same, so the cancel-yielded-tool
    endpoint treats them uniformly. The respond endpoint, by contrast,
    only flips a row that's still ``parked`` (the atomic
    ``mark_resumable`` guards against double-respond).
    """
    if sess.parked_status not in ("parked", "resumable"):
        return None
    return sess.parked_state or None


def _tool_call_id_for(blob: dict[str, Any]) -> str | None:
    """Where to find ``tool_call_id`` inside the parked_state blob.

    Worker writes it at the top level (M3+); older parks may have
    only had it inside ``yielded.resume_metadata``. Falling back keeps
    the lookup robust if a deployment carries old parks across the
    boundary.
    """
    tcid = blob.get("tool_call_id")
    if tcid:
        return tcid
    yielded = blob.get("yielded") or {}
    metadata = yielded.get("resume_metadata") or {}
    return metadata.get("tool_call_id")


def _graph_ask_user_dispatch(
    blob: dict[str, Any],
    *,
    tool_call_id: str | None = None,
) -> dict[str, Any] | None:
    """Return a graph ask_user dispatch entry from a graph checkpoint, or None.

    A graph ``tool_call`` node whose tool is the value-yielding ``ask_user``
    parks the session with the OUTER yield typed ``_approval`` (the graph park
    label), but the real ask_user prompt lives in the checkpoint's
    ``pending_dispatch`` as a ``{"kind": "ask_user", "tool_call_id",
    "resume_metadata": {prompt, response_schema, ...}}`` entry. The two
    yields.py endpoints consult this so a graph tool_call ask_user park is
    answerable over REST exactly like an agent-session ask_user park.

    ``tool_call_id`` (POST path) selects a specific pending entry; the GET path
    omits it and takes the first ask_user entry.
    """
    checkpoint = blob.get("graph_checkpoint")
    if not checkpoint:
        return None
    matches = [
        entry
        for entry in checkpoint.get("pending_dispatch") or []
        if entry.get("kind") == "ask_user"
        and (tool_call_id is None or entry.get("tool_call_id") == tool_call_id)
    ]
    if len(matches) > 1:
        # 01a0518f: two concurrent fan-out siblings can share a raw
        # provider tool_call_id; the REST wire contract has no other
        # field to disambiguate. First-match is the same ambiguity this
        # endpoint already had - logged so a real collision is visible
        # rather than silently resolving an arbitrary sibling.
        logger.warning(
            "_graph_ask_user_dispatch: %d pending entries share "
            "tool_call_id=%r; resolving the first",
            len(matches), tool_call_id,
        )
    return matches[0] if matches else None


def _pick_ask_user_gate(
    candidates: list[dict[str, Any]], *, token: str | None, session_id: str,
) -> dict[str, Any] | None:
    """The pending entry an ask_user respond answers, judged by the gate id it named (C-033).

    ``candidates`` are the entries that already match by ``tool_call_id``. No token: the first one, as before (counted ``absent``). A token:
    the candidate that carries it; when candidates exist but none does, the card is stale (409 ``approval_stale``, nothing moves).
    ``None`` when there is no candidate at all (the caller answers 404).
    """
    if not candidates:
        return None
    if token is None:
        count_gate_token(kind="ask_user", session_id=session_id, token=None)
        return candidates[0]
    for entry in candidates:
        if gate_id_of(entry.get("resume_metadata")) == token:
            count_gate_token(kind="ask_user", session_id=session_id, token=token)
            return entry
    count_gate_token(kind="ask_user", session_id=session_id, token=token, stale=True)
    raise stale_gate_error("ask_user")


def _cancel_kind(tool_name: str | None) -> str:
    """The noun a cancel's refusal uses: ``approval`` and ``ask_user`` are human gates, anything else (sleep, watch_files, an external wait) a ``yield``."""
    return {"_approval": "approval", "ask_user": "ask_user"}.get(tool_name or "", "yield")


def _fence_cancel(
    *,
    session_id: str,
    gate_id: str | None,
    expected_tool_name: str | None,
    tool_name: str | None,
    current_gate_id: str | None,
    queued: bool = False,
) -> None:
    """Refuse a cancel drawn for a yield or gate that is not the pending one (C-033), before anything is published.

    Two checks, both 409 ``approval_stale``. ``expected_tool_name`` is the kind of yield the card was drawn for: a yield that is not a human gate
    has no gate id, so this is all that stops a Skip left open for a sleep from cancelling an ask_user that reused the raw id. ``gate_id`` is the
    gate the card was drawn for: only an approval or an ask_user prompt has one; a cancel naming a gate that is not the pending one is refused, and
    for a yield that is not a gate (which has none) any id named is a mismatch. ``queued`` says the id named belongs to another pending entry of the
    same park (a sibling behind the one this route reaches), which changes the words, not the refusal. The decision is counted by
    :func:`_count_cancel` AFTER the approver check, so a refused cancel is not counted as a decision.
    """
    kind = _cancel_kind(tool_name)
    if expected_tool_name is not None and expected_tool_name != tool_name:
        if kind != "yield":
            count_gate_token(kind=kind, session_id=session_id, token=gate_id, stale=True)
        raise stale_gate_error("yield")
    if gate_id is not None and gate_id != current_gate_id:
        if kind != "yield":
            count_gate_token(kind=kind, session_id=session_id, token=gate_id, stale=True)
        raise stale_gate_error(kind, queued=queued)


def _count_cancel(*, session_id: str, gate_id: str | None, tool_name: str | None) -> None:
    """Count a cancel of a human gate that passed the fence and the approver check: ``matched`` with a token, ``absent`` without."""
    kind = _cancel_kind(tool_name)
    if kind != "yield":
        count_gate_token(kind=kind, session_id=session_id, token=gate_id)


async def _durable_wake(
    *,
    session: WorkspaceSession,
    event_key: str,
    payload: dict[str, Any],
    session_storage,
    engine: ClaimEngine | None,
    event_bus: EventBus,
    storage_provider=None,
) -> bool:
    """Durably flip the parked row to resumable, then wake the bus.

    D-C2 fix: the durable flip (stamp ``resume_event_payload`` +
    ``parked_status='resumable'`` + re-arm the claim lease) happens FIRST so a
    reply is never lost when the bus listener is down/reconnecting - LISTEN/
    NOTIFY is not durable, but the session row is, and the claim loop admits
    ``resumable`` rows without any bus. The bus publish that follows is only a
    best-effort immediate wake: durability is already guaranteed, so a bus
    hiccup must not fail an otherwise-accepted reply.

    Uses :func:`durably_wake_session`, which acts on the flip helper's bool:
    a guard-rejected row that is already ``resumable`` gets its claim lease
    re-armed, so a retry after a half-applied flip (row stamped, lease lost)
    repairs the row instead of handing back a 202 for a session the claim
    loop can never pick up. Returns True when this call advanced the row.
    """
    did = await durably_wake_session(
        session,
        event_key=event_key,
        payload=payload,
        session_storage=session_storage,
        engine=engine,  # type: ignore[arg-type]
    )
    if storage_provider is not None:
        from primer.events.wake import emit_session_wake

        await emit_session_wake(
            storage_provider, event_bus, event_key, payload,
        )
        return did
    try:
        await event_bus.publish(event_key, payload)
    except Exception:  # noqa: BLE001
        logger.exception(
            "ask_user resume publish failed for event_key=%r; durable flip "
            "already persisted, claim loop will recover", event_key,
        )
    return did


# ===========================================================================
# GET /v1/sessions/{id}/ask_user/pending
# ===========================================================================


class AskUserPendingResponse(BaseModel):
    """Operator-facing prompt payload."""

    tool_call_id: str = Field(...)
    prompt: str = Field(...)
    response_schema: dict[str, Any] | None = Field(default=None)
    gate_id: str | None = Field(
        default=None,
        description=(
            "The id of THIS prompt, minted when it was asked. Send it back as ``gate_id`` on respond: the provider's tool_call_id repeats "
            "across rounds, so an answer that names only the tool_call_id can answer a LATER question than the one the operator was "
            "reading (409 ``approval_stale`` when it names a prompt that has since been replaced). None for a park from before gates had ids."
        ),
    )
    parked_at: str = Field(
        ...,
        description=(
            "ISO-8601 timestamp the agent's turn parked on this "
            "prompt. UI can use it to surface a 'waiting for N "
            "seconds' affordance."
        ),
    )


@yields_router.get(
    "/sessions/{session_id}/ask_user/pending",
    response_model=AskUserPendingResponse,
    summary="Get the pending ask_user prompt (404 if none)",
    responses=common_responses(404, 500),
)
async def get_ask_user_pending(
    session_id: str = Path(...),
    session_storage=Depends(get_session_storage),
) -> AskUserPendingResponse:
    sess = await _load_session_or_404(session_storage, session_id)
    blob = _parked_blob(sess)
    if blob is None:
        raise NotFoundError(
            f"Session {session_id!r} has no pending ask_user prompt"
        )
    yielded = blob.get("yielded") or {}
    if yielded.get("tool_name") != "ask_user":
        # A graph tool_call ask_user park labels the outer yield ``_approval``;
        # the real ask_user prompt lives in the graph checkpoint. Surface it
        # so the operator-facing panel renders the same way as an agent park.
        graph_entry = _graph_ask_user_dispatch(blob)
        if graph_entry is not None:
            gmeta = graph_entry.get("resume_metadata") or {}
            return AskUserPendingResponse(
                tool_call_id=graph_entry.get("tool_call_id", ""),
                prompt=gmeta.get("prompt", ""),
                response_schema=gmeta.get("response_schema"),
                gate_id=gate_id_of(gmeta),
                parked_at=(
                    sess.parked_at.isoformat()
                    if sess.parked_at is not None
                    else gmeta.get("parked_at_iso", "")
                ),
            )
        raise NotFoundError(
            f"Session {session_id!r} is parked on a different tool"
        )
    metadata = yielded.get("resume_metadata") or {}
    tcid = _tool_call_id_for(blob)
    if not tcid:
        # A malformed park — log loudly, surface as 404 so the UI
        # doesn't loop on it.
        logger.warning(
            "ask_user park on session %s missing tool_call_id",
            session_id,
        )
        raise NotFoundError(
            f"Session {session_id!r} has a malformed ask_user park"
        )
    parked_at_iso = (
        sess.parked_at.isoformat()
        if sess.parked_at is not None
        else metadata.get("parked_at_iso", "")
    )
    return AskUserPendingResponse(
        tool_call_id=tcid,
        prompt=metadata.get("prompt", ""),
        response_schema=metadata.get("response_schema"),
        gate_id=gate_id_of(metadata),
        parked_at=parked_at_iso,
    )


# ===========================================================================
# POST /v1/sessions/{id}/ask_user/respond
# ===========================================================================


class AskUserRespondBody(BaseModel):
    """Operator's reply to an ask_user prompt."""

    tool_call_id: str = Field(...)
    gate_id: str | None = Field(
        default=None,
        pattern=GATE_ID_PATTERN,
        description=(
            "The ``gate_id`` the pending response served for the prompt this answers. Optional while clients catch up (an answer without it "
            "is accepted, logged and counted); an answer naming a prompt that is no longer the pending one is a 409 ``approval_stale`` and "
            "moves nothing."
        ),
    )
    response: Any = Field(
        ...,
        description=(
            "Operator-supplied value. May be a string, object, array, "
            "number, or boolean. Validated against the tool-supplied "
            "``response_schema`` when one was provided."
        ),
    )


def _validate_response_against_schema(
    *, response: Any, schema: dict[str, Any] | None,
) -> None:
    if schema is None:
        return
    # jsonschema arrives via mcp's transitive deps but we declare it
    # directly in pyproject for clarity.
    import jsonschema  # local import keeps the router import cheap
    from jsonschema import exceptions as jse

    try:
        jsonschema.validate(instance=response, schema=schema)
    except jse.ValidationError as exc:
        raise ValidationError(
            f"response failed schema validation: {exc.message}"
        ) from exc
    except jse.SchemaError as exc:
        # A bad schema is the tool author's bug, but we surface it as
        # a 422 so the UI can show a sensible message rather than 500.
        raise ValidationError(
            f"response_schema is invalid: {exc.message}"
        ) from exc


@yields_router.post(
    "/sessions/{session_id}/ask_user/respond",
    status_code=202,
    summary="Submit a response to a pending ask_user prompt",
    responses=common_responses(404, 409, 422, 500),
)
async def post_ask_user_respond(
    session_id: str = Path(...),
    body: AskUserRespondBody = Body(...),
    session_storage=Depends(get_session_storage),
    event_bus: EventBus = Depends(get_event_bus),
    engine: ClaimEngine | None = Depends(get_claim_engine),
    storage_provider=Depends(get_storage_provider),
) -> dict[str, str]:
    sess = await _load_session_or_404(session_storage, session_id)
    blob = _parked_blob(sess)
    if blob is None:
        raise NotFoundError(
            f"Session {session_id!r} has no pending ask_user prompt"
        )
    yielded = blob.get("yielded") or {}
    # Graph park: the outer yield is typed "_approval"; the real ask_user
    # nodes live in the checkpoint's pending_agent_yields. Match the
    # tool_call_id there and publish to that node's own event_key so a
    # graph agent-node ask_user can be answered over REST (not only the
    # channel path).
    checkpoint = blob.get("graph_checkpoint")
    if checkpoint:
        ay = _pick_ask_user_gate(
            [e for e in (checkpoint.get("pending_agent_yields") or [])
             if e.get("tool_call_id") == body.tool_call_id
             and e.get("tool_name") == "ask_user"],
            token=body.gate_id, session_id=session_id,
        )
        if ay is not None:
            ay_meta = ay.get("resume_metadata") or {}
            _validate_response_against_schema(
                response=body.response, schema=ay_meta.get("response_schema"),
            )
            ay_event_key = ay.get("event_key")
            if not ay_event_key:
                raise NotFoundError(
                    f"Session {session_id!r} agent yield is missing event_key"
                )
            await _durable_wake(
                session=sess,
                event_key=ay_event_key,
                # The wake names the gate it answers (C-033 round 2, PR 4): a redelivery after the session re-parked under the same key cannot answer a later one.
                payload=with_wake_gate({"response": body.response}, gate_id_of(ay_meta)),
                session_storage=session_storage,
                engine=engine,
                event_bus=event_bus,
                storage_provider=storage_provider,
            )
            return {"status": "accepted"}
        # A graph tool_call ask_user node: its prompt + schema live in
        # pending_dispatch; the wake key is the matching pending_toolcalls
        # entry's parked_event_key. Publishing the operator response there
        # lets the graph resume adapter feed it back as the node's result.
        disp = _graph_ask_user_dispatch(blob, tool_call_id=body.tool_call_id)
        tc = (
            _pick_ask_user_gate(
                [e for e in (checkpoint.get("pending_toolcalls") or [])
                 if e.get("tool_call_id") == body.tool_call_id],
                token=body.gate_id, session_id=session_id,
            )
            if disp is not None else None
        )
        if disp is None or tc is None:
            raise NotFoundError(
                f"No pending ask_user prompt with tool_call_id "
                f"{body.tool_call_id!r} on session {session_id!r}"
            )
        disp_meta = disp.get("resume_metadata") or {}
        _validate_response_against_schema(
            response=body.response, schema=disp_meta.get("response_schema"),
        )
        tc_event_key = tc.get("parked_event_key")
        if not tc_event_key:
            raise NotFoundError(
                f"Session {session_id!r} tool_call yield is missing event_key"
            )
        await _durable_wake(
            session=sess,
            event_key=tc_event_key,
            payload=with_wake_gate({"response": body.response}, gate_id_of(tc.get("resume_metadata"))),
            session_storage=session_storage,
            engine=engine,
            event_bus=event_bus,
            storage_provider=storage_provider,
        )
        return {"status": "accepted"}
    if yielded.get("tool_name") != "ask_user":
        raise NotFoundError(
            f"Session {session_id!r} is parked on a different tool"
        )
    expected = _tool_call_id_for(blob)
    if expected != body.tool_call_id:
        raise NotFoundError(
            f"No pending ask_user prompt with tool_call_id "
            f"{body.tool_call_id!r} on session {session_id!r}"
        )
    metadata = yielded.get("resume_metadata") or {}
    _pick_ask_user_gate([{"resume_metadata": metadata}], token=body.gate_id, session_id=session_id)
    _validate_response_against_schema(
        response=body.response, schema=metadata.get("response_schema"),
    )
    event_key = yielded.get("event_key")
    if not event_key:
        # Defensive — every park has one in current code.
        raise NotFoundError(
            f"Session {session_id!r} park is missing event_key"
        )
    await _durable_wake(
        session=sess,
        event_key=event_key,
        payload=with_wake_gate({"response": body.response}, gate_id_of(metadata)),
        session_storage=session_storage,
        engine=engine,
        event_bus=event_bus,
        storage_provider=storage_provider,
    )
    return {"status": "accepted"}


# ===========================================================================
# POST /v1/sessions/{id}/yields/{tool_call_id}/cancel
# ===========================================================================


class CancelYieldedToolBody(BaseModel):
    """Optional reason surfaced to the agent via YieldCancelled, and the gate this cancel is for."""

    gate_id: str | None = Field(
        default=None,
        pattern=GATE_ID_PATTERN,
        description=(
            "The ``gate_id`` the pending response served for the approval or ask_user prompt this cancels (C-033). Optional while clients "
            "catch up; a cancel naming a gate that is no longer the pending one is a 409 ``approval_stale`` and publishes nothing. A yield "
            "that is not a human gate (sleep, watch_files) has no gate id, so a cancel that names one is a mismatch and is refused the same "
            "way (409 ``approval_stale``)."
        ),
    )
    expected_tool_name: str | None = Field(
        default=None,
        max_length=128,
        description=(
            "The kind of yield the client drew this cancel for (``sleep``, ``watch_files``, ``ask_user``, ``_approval``, ``_external``). Optional "
            "while clients catch up. A yield that is not a human gate has no gate id to say which park a Skip was drawn for, and the provider "
            "repeats ``tool_call_id`` across rounds, so a Skip left open for a sleep could cancel an ask_user parked under the same id later. A "
            "cancel naming a kind that is not what is parked is a 409 ``approval_stale`` and publishes nothing."
        ),
    )
    reason: str | None = Field(
        default=None,
        max_length=1024,
        description=(
            "Free-text reason the operator skipped this yield. "
            "Passed verbatim into the tool's resume hook so the agent "
            "can surface it (or react to it) in its next turn."
        ),
    )


@yields_router.post(
    "/sessions/{session_id}/yields/{tool_call_id}/cancel",
    status_code=202,
    summary="Cancel one in-flight yield (tool-agnostic)",
    responses=common_responses(404, 409, 422, 500),
)
async def post_cancel_yielded_tool(
    session_id: str = Path(...),
    tool_call_id: str = Path(...),
    body: Annotated[
        CancelYieldedToolBody, Body(...)
    ] = CancelYieldedToolBody(),  # body is optional; default-construct
    session_storage=Depends(get_session_storage),
    event_bus: EventBus = Depends(get_event_bus),
    call_storage=Depends(get_external_tool_call_storage),
    user=Depends(require_user),
) -> dict[str, str]:
    """Cancel a single yield without terminating the whole session.

    An ``_approval`` gate is the exception to "tool-agnostic": its cancel payload is classified as a REJECTION, so cancelling it IS
    deciding it. It is therefore judged by the same approver check as the respond route (403 ``approver_mismatch`` for a user the gate's
    stamped spec does not admit), before anything is published. The spec is read from the pending entry resolved by ``tool_call_id`` (the one
    that carries the stamp), NOT from the top-level ``yielded`` projection, which for a graph park whose primary is a ToolCall node's
    approval holds ``original_call`` only. A graph park's top-level ``tool_name`` is ``"_approval"`` whatever its primary is, so only an
    entry whose own kind is ``_approval`` is judged (an agent's ``ask_user`` yield or an external wait is cancelled by its owner as
    before). A park that claims to be an approval but resolves to no entry is admin-only.

    Distinct from cancel-session (§9.2 of the spec): the tool's
    resume hook IS called with a :class:`YieldCancelled` payload and
    the agent's turn continues. If the session is already terminating
    via cancel-session (``cancel_requested=True``), this endpoint
    returns 409 — there's no point asking a tool to gracefully
    continue when the session is about to die anyway.
    """
    sess = await _load_session_or_404(session_storage, session_id)
    blob = _parked_blob(sess)
    if blob is None:
        raise NotFoundError(
            f"Session {session_id!r} has no in-flight yield"
        )
    expected = _tool_call_id_for(blob)
    if expected != tool_call_id:
        raise NotFoundError(
            f"No in-flight yield with tool_call_id {tool_call_id!r} "
            f"on session {session_id!r}"
        )
    if getattr(sess, "cancel_requested", False):
        raise ConflictError(
            f"Session {session_id!r} is terminating "
            "(cancel_requested=true); can't cancel an individual yield"
        )
    yielded = blob.get("yielded") or {}
    event_key = yielded.get("event_key")
    if not event_key:
        raise NotFoundError(
            f"Session {session_id!r} park is missing event_key"
        )
    gate = None
    if yielded.get("tool_name") == "_approval":
        # A graph park's top-level tool_name is "_approval" WHATEVER its primary is (`_build_pending_park_yield` hard-codes it), so
        # the real kind is on the pending entry: resolve the entry by tool_call_id alone (the enumeration order is the primary pick's,
        # so this is the entry whose key is published below) and judge only an approval. An unresolvable one cannot be shown to be
        # anything but an approval, so it is admin-only.
        gate = resolve_pending_gate(blob, tool_call_id=tool_call_id)
    resolved_tool = gate["kind"] if gate is not None else yielded.get("tool_name")
    current_gate_id = gate_id_of(gate["resume_metadata"] if gate is not None else yielded.get("resume_metadata"))
    _fence_cancel(
        session_id=session_id, gate_id=body.gate_id, expected_tool_name=body.expected_tool_name,
        tool_name=resolved_tool, current_gate_id=current_gate_id,
        queued=(
            body.gate_id is not None and body.gate_id != current_gate_id
            and any(
                e.get("event_key") != (gate or {}).get("event_key")
                and e.get("tool_call_id") == tool_call_id
                and gate_id_of(e.get("resume_metadata")) == body.gate_id
                for e in enumerate_pending_gates(blob)
            )
        ),
    )
    if yielded.get("tool_name") == "_approval":
        if gate is None:
            enforce_approvers(ADMIN_ONLY_METADATA, user)
        elif gate["kind"] == "_approval":
            enforce_approvers(gate["resume_metadata"], user)
    _count_cancel(session_id=session_id, gate_id=body.gate_id, tool_name=resolved_tool)
    # A cancel of a human gate names it too: it is a decision (an approval cancel is classified as a rejection), delivered by key alone and at least once.
    payload = with_wake_gate(
        make_cancelled_payload(reason=body.reason), current_gate_id if _cancel_kind(resolved_tool) != "yield" else None,
    )
    await event_bus.publish(event_key, payload)
    # An _external park additionally resolves its audit row so the
    # pending endpoints and the global list reflect the cancel.
    if yielded.get("tool_name") == "_external":
        from primer.session.external_calls import flip_external_row

        meta = yielded.get("resume_metadata") or {}
        await flip_external_row(
            call_storage,
            row_id=meta.get("external_call_row_id"),
            status="cancelled",
            result={
                "cancelled": True,
                "reason": body.reason or "cancelled by operator",
            },
        )
    return {"status": "accepted"}


__all__ = [
    "AskUserPendingResponse",
    "AskUserRespondBody",
    "CancelYieldedToolBody",
    "get_ask_user_pending",
    "post_ask_user_respond",
    "post_cancel_yielded_tool",
    "yields_router",
]
