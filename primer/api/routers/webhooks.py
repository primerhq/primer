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
- Dispatch is fire-and-forget via a BackgroundTask; the 202 is returned
  immediately.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from primer.api.deps import (
    get_claim_engine,
    get_event_bus,
    get_storage_provider,
)
from primer.trigger.dispatch import fire_trigger
from primer.trigger.fire_id import make_fire_id
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


async def _dispatch_webhook(
    trigger_id: str,
    extra_context: dict,
    storage_provider: Any,
    event_bus: Any,
    claim_engine: Any = None,
    scheduler: Any = None,
    workspace_registry: Any = None,
) -> None:
    """Background task: fire subscriptions for a received webhook.

    ``claim_engine`` / ``scheduler`` / ``workspace_registry`` are resolved
    from ``app.state`` by the request handler and threaded through here.
    They MUST be real for the fresh-session subscription kinds
    (``agent_fresh_session`` / ``graph_fresh_session``), which create an
    ``auto_start=True`` session: a ``claim_engine=None`` there flips the
    session to RUNNING but never claims it, hanging it forever (now a
    loud ConfigError at create time rather than a silent hang).
    """
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
    except Exception:
        logger.exception("webhook dispatch failed for trigger %s", trigger_id)


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
    delivery_id = make_fire_id(trigger.id, fired_at)

    extra_context = {
        "webhook_body": body_str,
        "webhook_headers": _filter_headers(dict(request.headers)),
        "webhook_query": dict(request.query_params),
        "webhook_method": request.method,
    }

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
    background_tasks.add_task(
        _dispatch_webhook,
        trigger.id,
        extra_context,
        sp,
        event_bus,
        claim_engine,
        scheduler,
        workspace_registry,
    )

    return {"delivery_id": delivery_id, "status": "accepted"}


__all__ = ["webhooks_router"]
