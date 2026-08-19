"""Public webhook inbound endpoint -- POST /v1/webhooks/{token}.

This router is mounted WITHOUT the auth dependency so any HTTP client can
reach it. The capability token in the URL path serves as the primary
authenticator; an optional HMAC-SHA256 signature header provides an
additional layer.

Security model:
- The token is a 32-hex-char server-minted secret URL component. It is
  never logged in full and never returned in responses after creation.
- When ``hmac_secret`` is set on the trigger config, every inbound
  request MUST carry ``X-Primer-Signature: sha256=<hex>`` computed over
  the raw request body. Mismatches are rejected 401.
- A 403 is returned if the trigger is disabled.
- A 404 is returned if no trigger matches the token.
- The body is capped at 1 MB. Larger payloads are rejected 413.
- Rate limiting is per-token: 60 requests per minute (sliding window,
  in-process; approximate in multi-worker deployments).
- Internal errors are never surfaced to the caller -- a generic 500 body
  is returned and the detail is logged server-side only.

Payload delivery:
- The webhook payload is passed as ``extra_context`` to ``fire_trigger``
  which merges it into the fire_context. Dispatchers and payload
  templates can reference ``webhook_body``, ``webhook_headers``,
  ``webhook_query``, and ``webhook_method``.
- A durable ``WebhookDelivery`` row (status ``pending``) is written
  BEFORE the 202 is returned; the BackgroundTask dispatches immediately
  and marks the row ``done``/``failed``. A crash between the 202 and
  dispatch completion leaves the row ``pending``, and startup recovery
  re-dispatches stale pending rows (senders never retry a 202). Delivery
  is therefore at-least-once rather than fire-and-forget.
- At-least-once is NOT idempotent. Every inbound POST gets its own
  delivery row and its own dispatch, so a sender that retries a POST
  fires the trigger a second time, and a recovery re-fire can
  double-deliver when the process died after dispatching but before
  marking the row. Suppressing genuine duplicates would require a
  sender-supplied idempotency key to dedupe on; that is out of scope.
"""

from __future__ import annotations

import hashlib
import hmac
import asyncio
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse

from primer.api.deps import (
    get_claim_engine,
    get_event_bus,
    get_storage_provider,
)
from primer.trigger.dispatch import fire_trigger
from primer.trigger.fire_id import make_fire_id
from primer.trigger.hold import HOLD_MAX_SECONDS, HeldFire, fire_and_hold
from primer.trigger.service import (
    ServiceDeps,
    WebhookTokenNotFound,
    get_trigger_by_webhook_token,
)
from primer.trigger.subscribers import DispatchDeps

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BODY_LIMIT_BYTES = 1 * 1024 * 1024  # 1 MB

_RATE_LIMIT_MAX = 60
_RATE_LIMIT_WINDOW_SECS = 60

_HEADER_BLOCKLIST = frozenset({
    "authorization",
    "cookie",
    "set-cookie",
    "x-primer-signature",
    "proxy-authorization",
    "x-forwarded-for",
    "x-real-ip",
    "transfer-encoding",
    "connection",
})

# ---------------------------------------------------------------------------
# In-process per-token rate limiter (sliding window)
# ---------------------------------------------------------------------------

_rate_windows: dict[str, list[float]] = defaultdict(list)


def _check_rate_limit(token: str) -> bool:
    """Return True if the request is within the rate limit, False if exceeded."""
    now = time.monotonic()
    cutoff = now - _RATE_LIMIT_WINDOW_SECS
    _rate_windows[token] = [t for t in _rate_windows[token] if t > cutoff]
    if len(_rate_windows[token]) >= _RATE_LIMIT_MAX:
        return False
    _rate_windows[token].append(now)
    return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _filter_headers(headers: dict) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in _HEADER_BLOCKLIST}


def _verify_hmac(secret: str, body: bytes, sig_header: str | None) -> bool:
    """Verify HMAC-SHA256 over *body* against *sig_header*.

    Accepts both ``sha256=<hex>`` and bare ``<hex>`` forms.
    """
    if not sig_header:
        return False
    candidate = sig_header.removeprefix("sha256=")
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(candidate, expected)


async def _finalize_delivery(
    storage_provider: Any,
    delivery_id: str,
    *,
    ok: bool,
    fire_id: str | None = None,
    results: list[dict] | None = None,
) -> None:
    """Best-effort flip of a WebhookDelivery row to done/failed.

    Never raises: durability marking is advisory and must not turn a
    successful dispatch into a logged failure (or vice versa). A missing
    row (create failed earlier) is tolerated silently.

    Deliberately does NOT touch ``attempts``: that counts attempts STARTED
    and is written by whoever starts one (the endpoint on create, startup
    recovery before each re-dispatch). Incrementing here would both
    double-count every recovered attempt and, because this marking is
    swallowed on failure, fail to count the very cases the attempt cap
    guards against.
    """
    from primer.model.webhook_delivery import WebhookDelivery

    try:
        storage = storage_provider.get_storage(WebhookDelivery)
        row = await storage.get(delivery_id)
        if row is None:
            return
        updates: dict[str, Any] = {
            "status": "done" if ok else "failed",
            "completed_at": datetime.now(timezone.utc),
        }
        if fire_id is not None:
            updates["fire_id"] = fire_id
        if results is not None:
            updates["results"] = results
        await storage.update(row.model_copy(update=updates))
    except Exception:  # noqa: BLE001 -- advisory marking, never fatal
        logger.debug(
            "webhook delivery finalize failed for %s", delivery_id,
            exc_info=True,
        )


async def _finalize_hold(
    storage_provider: Any, delivery_id: str | None, held: HeldFire,
) -> None:
    """Write a completed hold's outcome onto its delivery row."""
    if delivery_id is None:
        return
    await _finalize_delivery(
        storage_provider,
        delivery_id,
        ok=not held.timed_out,
        fire_id=held.fire_result.fire_id if held.fire_result else None,
        results=held.results,
    )


async def _finalize_when_done(
    storage_provider: Any, delivery_id: str | None, hold_task: Any,
) -> None:
    """Await a hold that outlived its wait cap, then record its result.

    Runs as a BackgroundTask after the 202 has been sent, so the poll
    endpoint eventually serves the real result instead of a row stuck at
    'pending'.
    """
    try:
        held = await hold_task
    except Exception:  # noqa: BLE001 - the response already went out
        logger.exception("webhook hold task failed for %s", delivery_id)
        if delivery_id is not None:
            await _finalize_delivery(storage_provider, delivery_id, ok=False)
        return
    await _finalize_hold(storage_provider, delivery_id, held)


async def _dispatch_webhook(
    trigger_id: str,
    extra_context: dict,
    storage_provider: Any,
    event_bus: Any,
    claim_engine: Any = None,
    scheduler: Any = None,
    workspace_registry: Any = None,
    *,
    delivery_id: str | None = None,
) -> None:
    """Background task: fire subscriptions for a received webhook.

    ``claim_engine`` / ``scheduler`` / ``workspace_registry`` are resolved
    from ``app.state`` by the request handler and threaded through here.
    They MUST be real for the fresh-session subscription kinds
    (``agent_fresh_session`` / ``graph_fresh_session``), which create an
    ``auto_start=True`` session: a ``claim_engine=None`` there flips the
    session to RUNNING but never claims it, hanging it forever (now a
    loud ConfigError at create time rather than a silent hang).

    When ``delivery_id`` names a persisted :class:`WebhookDelivery` row,
    the row is marked done/failed after the dispatch completes (best
    effort). This is the same code path startup recovery re-runs for a
    stale ``pending`` row, so both the live fire and the crash-recovery
    fire share one dispatch implementation.
    """
    ok = False
    try:
        dispatch_deps = DispatchDeps(
            storage_provider=storage_provider,
            claim_engine=claim_engine,
            scheduler=scheduler,
            workspace_registry=workspace_registry,
            event_bus=event_bus,
        )
        result = await fire_trigger(
            trigger_id=trigger_id,
            scheduled_for=None,
            deps=dispatch_deps,
            extra_context=extra_context,
        )
        logger.info(
            "webhook dispatched trigger=%s fire_id=%s dispatched=%d",
            trigger_id,
            result.fire_id,
            len(result.results),
        )
        ok = True
    except Exception:
        logger.exception("webhook dispatch failed for trigger %s", trigger_id)
    if delivery_id is not None:
        await _finalize_delivery(storage_provider, delivery_id, ok=ok)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

webhooks_router = APIRouter(tags=["webhooks"])


@webhooks_router.post(
    "/v1/webhooks/{token}",
    status_code=202,
    summary="Receive an inbound webhook",
    include_in_schema=True,
)
async def receive_webhook(
    token: str,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict:
    """Accept a webhook POST and dispatch the associated trigger's subscriptions.

    Returns 202 immediately; subscriptions are dispatched asynchronously.
    """
    # Rate limit
    if not _check_rate_limit(token):
        raise HTTPException(
            status_code=429,
            detail={"code": "rate_limited", "message": "Too many requests for this webhook"},
        )

    # Body size cap
    body = await request.body()
    if len(body) > _BODY_LIMIT_BYTES:
        raise HTTPException(
            status_code=413,
            detail={"code": "payload_too_large", "message": "Request body exceeds 1 MB limit"},
        )

    # Resolve trigger by token
    sp = get_storage_provider(request)
    service_deps = ServiceDeps(storage_provider=sp)
    try:
        trigger = await get_trigger_by_webhook_token(token=token, deps=service_deps)
    except WebhookTokenNotFound:
        raise HTTPException(
            status_code=404,
            detail={"code": "webhook_not_found", "message": "No webhook found for this token"},
        )
    except Exception:
        logger.exception("webhook token lookup error")
        raise HTTPException(
            status_code=500,
            detail={"code": "internal_error", "message": "Internal error"},
        )

    # Enabled check
    if not trigger.enabled:
        raise HTTPException(
            status_code=403,
            detail={"code": "webhook_disabled", "message": "This webhook trigger is disabled"},
        )

    # HMAC verification
    hmac_secret = trigger.config.hmac_secret
    if hmac_secret is not None:
        sig_header = request.headers.get("x-primer-signature")
        if not _verify_hmac(hmac_secret.get_secret_value(), body, sig_header):
            raise HTTPException(
                status_code=401,
                detail={"code": "hmac_mismatch", "message": "HMAC signature verification failed"},
            )

    # Build payload extras for the fire_context
    try:
        body_str = body.decode("utf-8", errors="replace")
    except Exception:
        body_str = ""

    fired_at = datetime.now(timezone.utc)
    # The row id must be unique PER REQUEST. It used to be the bare fire_id
    # (``fire-{trigger_id}-{ms}``), which keys on (trigger, arrival
    # millisecond) and correlates with NEITHER the sender nor the payload:
    # two DISTINCT events for one trigger in the same millisecond collided on
    # the primary key, and the loser was silently accepted without dispatching
    # (losing its body outright). Keep the fire_id as a readable correlation
    # prefix and append a random suffix so distinct requests never collide.
    delivery_id = f"{make_fire_id(trigger.id, fired_at)}-{uuid4().hex[:8]}"

    extra_context = {
        "webhook_body": body_str,
        "webhook_headers": _filter_headers(dict(request.headers)),
        "webhook_query": dict(request.query_params),
        "webhook_method": request.method,
    }

    # Durability: persist a pending WebhookDelivery BEFORE returning 202
    # and BEFORE the fire-and-forget BackgroundTask. A crash between the
    # 202 and dispatch completion leaves this row 'pending'; startup
    # recovery re-dispatches stale pending rows (senders never retry a
    # 202).
    #
    # This does NOT make the endpoint idempotent. Every inbound POST gets
    # its own row and its own dispatch, so a genuine duplicate (a sender
    # retrying seconds later) lands a different fire_id, hits no conflict,
    # and fires the trigger again. Delivery is at-least-once; real
    # duplicate suppression would need a sender-supplied idempotency key
    # to dedupe on, which is out of scope.
    #
    # Best effort: if the write fails we still dispatch (behaviour then
    # matches the old fire-and-forget path - no worse than before).
    from primer.model.except_ import ConflictError
    from primer.model.webhook_delivery import WebhookDelivery

    delivery_persisted = False
    try:
        await sp.get_storage(WebhookDelivery).create(WebhookDelivery(
            id=delivery_id,
            trigger_id=trigger.id,
            extra_context=extra_context,
            status="pending",
            created_at=fired_at,
            # The BackgroundTask queued below is attempt 1. Recording it here
            # (before it runs) is what lets the recovery sweep's attempt cap
            # bound a delivery whose dispatch keeps killing the process.
            attempts=1,
        ))
        delivery_persisted = True
    except ConflictError:
        # Not expected now that ids carry a random suffix. Treat it as the
        # best-effort persist failure it is and dispatch anyway: returning
        # an "accepted" 202 without dispatching would drop the delivery,
        # which is the exact silent loss this durability path exists to
        # eliminate.
        logger.exception(
            "webhook delivery %s conflicted on create; dispatching without "
            "durability", delivery_id,
        )
    except Exception:
        logger.exception(
            "webhook delivery persist failed for %s; dispatching without "
            "durability", delivery_id,
        )

    # Fire and forget. Resolve the live claim_engine / scheduler /
    # workspace_registry from app.state HERE (request scope, where app.state
    # is reachable) and thread them into the background task. The
    # fresh-session subscription dispatchers create auto_start sessions that
    # require a real ClaimEngine; passing None used to flip a session to
    # RUNNING with no claimer (silent hang). Resolution is best-effort: a
    # deployment that runs without these wired simply has no fresh-session
    # subscriptions to dispatch (the create_session guard raises loudly if
    # one is attempted).
    event_bus = get_event_bus(request)
    claim_engine = get_claim_engine(request)
    scheduler = getattr(request.app.state, "scheduler", None)
    workspace_registry = getattr(request.app.state, "workspace_registry", None)
    row_id = delivery_id if delivery_persisted else None

    if getattr(trigger.config, "interactive", False):
        # Interactive: hold the response until the fired run(s) terminate.
        # The hold runs as its own task so the caller-visible wait cap can
        # expire without abandoning the wait: past the cap the task keeps
        # running and a BackgroundTask records its result on the row.
        hold_task = asyncio.create_task(
            fire_and_hold(
                trigger_id=trigger.id,
                extra_context=extra_context,
                deps=DispatchDeps(
                    storage_provider=sp,
                    claim_engine=claim_engine,
                    scheduler=scheduler,
                    workspace_registry=workspace_registry,
                    event_bus=event_bus,
                ),
                workspace_registry=workspace_registry,
                wait_timeout=HOLD_MAX_SECONDS,
            ),
            name=f"webhook-hold-{delivery_id}",
        )
        cap = float(getattr(trigger.config, "wait_timeout_seconds", 60))
        done, _still_running = await asyncio.wait({hold_task}, timeout=cap)
        if hold_task in done:
            held = hold_task.result()
            await _finalize_hold(sp, row_id, held)
            if not held.timed_out:
                return JSONResponse(status_code=200, content={
                    "fire_id": (
                        held.fire_result.fire_id if held.fire_result else None
                    ),
                    "results": held.results,
                })
        else:
            background_tasks.add_task(
                _finalize_when_done, sp, row_id, hold_task,
            )
        return JSONResponse(status_code=202, content={
            "delivery_id": delivery_id, "status": "pending",
        })

    background_tasks.add_task(
        _dispatch_webhook,
        trigger.id,
        extra_context,
        sp,
        event_bus,
        claim_engine,
        scheduler,
        workspace_registry,
        delivery_id=row_id,
    )

    return {"delivery_id": delivery_id, "status": "accepted"}


@webhooks_router.get(
    "/v1/webhooks/{token}/deliveries/{delivery_id}",
    summary="Poll one webhook delivery for its result",
    include_in_schema=True,
)
async def get_webhook_delivery(
    token: str,
    delivery_id: str,
    request: Request,
) -> dict:
    """Return a delivery's status and, once complete, its run results.

    Same capability token and same per-token rate-limit bucket as the POST
    (S6 section 4), so a caller that received a 202 can finish the exchange
    with the credentials it already has.
    """
    if not _check_rate_limit(token):
        raise HTTPException(
            status_code=429,
            detail={
                "code": "rate_limited",
                "message": "Too many requests for this webhook",
            },
        )
    sp = get_storage_provider(request)
    try:
        trigger = await get_trigger_by_webhook_token(
            token=token, deps=ServiceDeps(storage_provider=sp),
        )
    except WebhookTokenNotFound:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "webhook_not_found",
                "message": "No webhook found for this token",
            },
        )
    from primer.model.webhook_delivery import WebhookDelivery

    row = await sp.get_storage(WebhookDelivery).get(delivery_id)
    if row is None or row.trigger_id != trigger.id:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "delivery_not_found",
                "message": "No delivery found for this webhook",
            },
        )
    return {
        "delivery_id": row.id,
        "status": row.status,
        "fire_id": row.fire_id,
        "results": row.results or [],
    }


__all__ = ["webhooks_router"]
