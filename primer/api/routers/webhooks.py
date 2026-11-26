"""Public webhook inbound endpoint -- POST /v1/webhooks/{token}.

This router is mounted WITHOUT the auth dependency so any HTTP client can
reach it. The capability token in the URL path serves as the primary
authenticator; an optional HMAC-SHA256 signature header provides an
additional layer.

Security model:
- The token is a 32-hex-char server-minted secret URL component. It is
  never logged in full and never returned in responses after creation.
- When ``hmac_secret`` is set on the trigger config, every inbound
  request MUST carry ``X-Primer-Signature: sha256=<hex>``. With the
  recommended ``X-Primer-Timestamp`` header (unix seconds, within a
  5-minute window) the signature is computed over ``"<ts>\\n<raw body>"``;
  the timestamp is inside the MAC, so a captured request cannot be
  replayed past the window, and an exact replay inside the window is
  refused by a short-lived signature cache with two levels: the
  per-process in-memory dict (the fast path) and a row in the shared
  store (a conditional insert keyed on (trigger id, canonical
  signature), expiring with the signature's own timestamp), which is
  what makes the refusal hold across API replicas.
- The pre-timestamp body-only signature (HMAC of the raw body) is
  accepted only while the trigger's ``legacy_body_hmac`` setting allows
  it: stored rows that predate the flag load ``True`` so deployed
  senders keep working, but a trigger created from now on stores
  ``False`` (its creator must set the flag to keep body-only) and
  refuses body-only signatures with ``timestamp_required``.
- Refusals answer 401 with a code: ``hmac_mismatch`` (missing or wrong
  signature), ``timestamp_invalid`` (non-numeric header),
  ``timestamp_stale`` (outside the window), ``timestamp_required``
  (body-only refused for this trigger), ``replayed`` (an accepted
  signed request resubmitted inside the window).
- A 403 is returned if the trigger is disabled.
- A 404 is returned if no trigger matches the token.
- The body is capped at 1 MB, checked BEFORE the read: a declared
  Content-Length over the cap is refused without reading a byte, and a
  streamed body is read only until it crosses the cap. Larger payloads
  are rejected 413.
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

from primer.api.route_tools import route_tool

import hashlib
import hmac
import asyncio
import logging
import time
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
from primer.model.except_ import ConflictError
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

#: How long a signed (timestamped) webhook request stays replayable. The
#: same window the signature's ``X-Primer-Timestamp`` must stay inside: a
#: captured request older than this is refused ``timestamp_stale`` anyway,
#: so the replay cache only has to hold signatures that are still valid.
_SIGNATURE_WINDOW_SECONDS = 300

#: Recently ACCEPTED timestamped signatures, keyed
#: ``"<trigger_id>:<expected hex>"`` (the canonical signature the server
#: computed, so the "sha256=" prefix form cannot dodge it), value: the
#: WALL-CLOCK moment the signature's own timestamp stops being valid
#: (``int(ts) + _SIGNATURE_WINDOW_SECONDS``). An exact resubmission of a
#: captured request re-computes the same hex while its timestamp still
#: validates and is refused ``replayed``. The entry must live as long as
#: the timestamp itself (a future-dated ts stays valid past acceptance),
#: and a refusal never removes an unexpired entry (otherwise every second
#: replay would be accepted). Body-only (legacy) signatures are NOT
#: cached: they carry no window, and a sender legitimately resending the
#: same unsigned body must keep working. This dict is the FIRST-LEVEL
#: check only (per process, cheap); the cross-replica arbiter is the
#: shared-store conditional insert in :func:`_claim_signature`. Bounded by
#: :func:`_evict_replayed`: a dropped entry can only make an old replay
#: accepted again, never a fresh request refused.
_replayed_signatures: dict[str, float] = {}
_REPLAY_CACHE_MAX_KEYS = 10_000
#: How many expired shared-store rows one accepted webhook may prune: a burst
#: after a quiet period must not add an unbounded number of round trips to
#: one request's latency; the rest wait for the next accept.
_PRUNE_MAX_PER_REQUEST = 100


def _evict_replayed(now: float) -> None:
    """Keep the replay cache bounded (wall clock).

    Expired entries go first. If it is still over the bound, trim the
    trigger holding the MOST entries from its oldest: the bound is global,
    so one user with many triggers must not push another trigger's
    still-valid entry out (round 1 M4).
    """
    for key in [k for k, expiry in _replayed_signatures.items() if expiry <= now]:
        del _replayed_signatures[key]
    if len(_replayed_signatures) <= _REPLAY_CACHE_MAX_KEYS:
        return
    per_trigger: dict[str, list[str]] = {}
    for key in _replayed_signatures:
        per_trigger.setdefault(key.split(":", 1)[0], []).append(key)
    while len(_replayed_signatures) > _REPLAY_CACHE_MAX_KEYS:
        keys = per_trigger[max(per_trigger, key=lambda t: len(per_trigger[t]))]
        del _replayed_signatures[keys.pop(0)]  # that trigger's oldest entry


async def _claim_signature(
    storage_provider: Any,
    trigger_id: str,
    secret: str,
    body: bytes,
    ts_header: str,
) -> bool:
    """Cross-replica replay guard (review 2026-11-23 6.11 follow-up): record an
    accepted timestamped signature in the shared store.

    True when this replica recorded it; False when an unexpired row for the
    SAME (trigger id, canonical hex) is already committed - the request is
    then a resubmission of a captured request accepted by another replica
    and must be refused ``replayed``. The row id is derived from the key
    pair, so two replicas' inserts conflict on the primary key on every
    backend (the ``claim_thread`` pattern); the in-memory
    ``_replayed_signatures`` dict is the first-level check, and only this
    insert is authoritative across replicas.

    Expired rows are pruned first, best effort: a conflicting row's expiry
    is the resubmitter's OWN timestamp plus the window, and the timestamp
    was validated inside the window just before, so a conflict here is
    always a live refusal.
    """
    from primer.model.storage import Op
    from primer.model.webhook_signature import WebhookSignature
    from primer.storage.q import Q

    expected = hmac.new(
        secret.encode(), f"{ts_header}\n".encode() + body, hashlib.sha256,
    ).hexdigest()
    storage = storage_provider.get_storage(WebhookSignature)
    now_dt = datetime.now(timezone.utc)
    # The storage layer holds datetimes in the pydantic dump's ISO form
    # (UTC as ``...Z``) and compares that text lexicographically, so the
    # prune bound is the wall clock as whole-second ISO text (the same
    # convention as primer/bus/scheduler_tasks.py's due-parked predicate).
    bound = now_dt.astimezone(timezone.utc).replace(microsecond=0)
    bound = bound.isoformat().replace("+00:00", "Z")
    # Bounded per request: a burst after a quiet period must not add an
    # unbounded number of round trips to one webhook's latency; the rows
    # past the bound stay harmless and go on the next accept.
    try:
        stale_ids = [
            row_id for row_id, _ in await storage.find_ids(
                Q(WebhookSignature).where_op("expires_at", Op.LE, bound).build(),
            )
        ][: _PRUNE_MAX_PER_REQUEST]
        for row_id in stale_ids:
            await storage.delete(row_id)
    except Exception:  # noqa: BLE001 - pruning is best effort; the insert is the arbiter
        logger.debug("webhook replay cache prune failed", exc_info=True)
    digest = hashlib.sha256(
        f"{trigger_id}\0{expected}".encode(),
    ).hexdigest()[:32]
    try:
        await storage.create(WebhookSignature(
            id=f"webhook-signature-{digest}",
            trigger_id=trigger_id,
            signature=expected,
            expires_at=datetime.fromtimestamp(
                int(ts_header) + _SIGNATURE_WINDOW_SECONDS, tz=timezone.utc,
            ),
        ))
    except ConflictError:
        return False
    return True


#: The 401 message per refusal code (the code is the contract; the message
#: is for the sender reading a 401 by hand).
_SIGNATURE_REFUSAL_MESSAGES = {
    "hmac_mismatch": "HMAC signature verification failed",
    "timestamp_invalid": "X-Primer-Timestamp must be unix seconds",
    "timestamp_stale": "X-Primer-Timestamp is outside the 5 minute window",
    "timestamp_required": "X-Primer-Timestamp is required for this trigger",
    "replayed": "This signed request was already accepted",
}

_HEADER_BLOCKLIST = frozenset({
    "authorization",
    "cookie",
    "set-cookie",
    "x-primer-signature",
    "x-primer-timestamp",
    "proxy-authorization",
    "x-forwarded-for",
    "x-real-ip",
    "transfer-encoding",
    "connection",
})

# ---------------------------------------------------------------------------
# In-process per-token rate limiter (sliding window)
# ---------------------------------------------------------------------------

#: Least recently used first. The key is the caller-chosen path token, checked before the token is looked up, so
#: the map is bounded (W45): past the cap the least recently used token's window is dropped.
_rate_windows: dict[str, list[float]] = {}
_RATE_LIMIT_MAX_KEYS = 10_000


def _check_rate_limit(token: str) -> bool:
    """Return True if the request is within the rate limit, False if exceeded."""
    now = time.monotonic()
    cutoff = now - _RATE_LIMIT_WINDOW_SECS
    window = [t for t in _rate_windows.pop(token, ()) if t > cutoff]
    allowed = len(window) < _RATE_LIMIT_MAX
    if allowed:
        window.append(now)
    _rate_windows[token] = window  # re-inserted: now the most recently used
    while len(_rate_windows) > _RATE_LIMIT_MAX_KEYS:
        del _rate_windows[next(iter(_rate_windows))]
    return allowed


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _filter_headers(headers: dict) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in _HEADER_BLOCKLIST}


def _verify_webhook_signature(
    secret: str,
    body: bytes,
    sig_header: str | None,
    ts_header: str | None,
    *,
    trigger_id: str,
    legacy_body_only: bool,
) -> tuple[bool, str | None]:
    """Verify the inbound signature and refuse replays.

    Returns ``(ok, code)``: on refusal ``code`` is one of
    ``timestamp_invalid``, ``timestamp_stale``, ``hmac_mismatch``,
    ``timestamp_required`` or ``replayed``.

    With ``ts_header`` (unix seconds) present, the signature covers
    ``"<ts>\\n<raw body>"`` and the timestamp is checked (strictly parsed,
    within ``_SIGNATURE_WINDOW_SECONDS``) BEFORE the MAC: a stale request
    is refused without doing the work of a signature check. Without it, the
    signature covers the body only (the pre-timestamp scheme), accepted
    only while ``legacy_body_only`` allows it. Both forms accept
    ``sha256=<hex>`` and bare ``<hex>``. An accepted TIMESTAMPED
    signature is recorded per trigger until its OWN timestamp stops being
    valid (``int(ts) + window``, wall clock), so an exact resubmission
    while the timestamp still validates is refused ``replayed``; the
    lookup never removes an unexpired entry. Accepted body-only signatures
    are not (they carry no window and a legitimate resend must work).
    """
    if ts_header is not None:
        # Strict parse: isdigit() alone lets "²" through (int() raises)
        # and 400/5000-digit strings (OverflowError / the int digit
        # limit), which answered 500 with a logged traceback (round 1
        # I2). Unix seconds fit in 10 digits; 12 leaves headroom for
        # far-future clocks.
        if not (ts_header.isascii() and ts_header.isdigit() and len(ts_header) <= 12):
            return False, "timestamp_invalid"
        ts_value = int(ts_header)
        if abs(time.time() - ts_value) > _SIGNATURE_WINDOW_SECONDS:
            return False, "timestamp_stale"
        expected = hmac.new(
            secret.encode(), f"{ts_header}\n".encode() + body, hashlib.sha256,
        ).hexdigest()
    else:
        if not legacy_body_only:
            return False, "timestamp_required"
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    if not sig_header:
        return False, "hmac_mismatch"
    candidate = sig_header.removeprefix("sha256=")
    # Bytes, not str: compare_digest raises TypeError on a non-ASCII str, which answered 500 (W46). "replace" turns
    # anything unencodable (a lone surrogate) into "?", which is never hex, so it can only mismatch.
    if not hmac.compare_digest(candidate.encode("utf-8", "replace"), expected.encode()):
        return False, "hmac_mismatch"

    if ts_header is not None:
        # Accepted: record it. Keyed on the server-computed (canonical)
        # hex, per trigger: the same secret + body signed for a DIFFERENT
        # trigger is not a replay of this one. The entry lives until the
        # timestamp ITSELF stops being valid (round 1 I1), and the lookup
        # never removes an unexpired entry - a refusal must not consume
        # it, or every second replay would be accepted (round 1 C1).
        key = f"{trigger_id}:{expected}"
        now = time.time()
        if _replayed_signatures.get(key, 0.0) > now:
            return False, "replayed"
        _replayed_signatures[key] = ts_value + _SIGNATURE_WINDOW_SECONDS
        _evict_replayed(now)
    return True, None


def _payload_too_large() -> HTTPException:
    return HTTPException(
        status_code=413,
        detail={"code": "payload_too_large", "message": "Request body exceeds 1 MB limit"},
    )


async def _read_body_capped(request: Request, limit: int) -> bytes:
    """Read the request body, refusing at *limit* before buffering it in full.

    A declared ``Content-Length`` over the limit is refused without reading
    a single byte (review 4.4: the old check ran only AFTER ``request.body()``
    had already buffered whatever the app-wide cap allowed). Otherwise the
    body is read chunk by chunk and the read stops, refusing, as soon as the
    total crosses the limit, so an over-limit streamed body is never held in
    memory in full.
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                raise _payload_too_large()
        except ValueError:
            # A non-numeric declared length: fall through and count the bytes.
            pass
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise _payload_too_large()
    return bytes(body)


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


async def _record_fire(storage_provider: Any, delivery_id: str | None, fire_id: str | None) -> None:
    """Record on a still-pending delivery row that its trigger has fired (W12).

    The row stays ``pending`` (the poll endpoint reports the run as going) but carries the ``fire_id``, which the
    recovery sweep reads as "already fired": an interactive hold keeps its row pending for as long as the run lasts,
    well past the sweep's grace. A field patch guarded on ``pending``, so it never reopens or overwrites a finished
    row. Best effort, like the finalize.
    """
    if delivery_id is None or fire_id is None:
        return
    from primer.model.webhook_delivery import WebhookDelivery

    try:
        await storage_provider.get_storage(WebhookDelivery).patch_if(
            delivery_id, {"fire_id": fire_id}, where={"status": ["pending"]},
        )
    except Exception:  # noqa: BLE001 -- advisory marking, never fatal
        logger.debug("webhook delivery fire record failed for %s", delivery_id, exc_info=True)


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
        await _record_fire(storage_provider, delivery_id, result.fire_id)
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
    # A JSON binding would re-encode the payload and invalidate its HMAC.
    # Keep bytes opaque; generated tools use the shared base64 transport.
    openapi_extra={
        "requestBody": {
            "required": False,
            "description": "Raw webhook bytes. Sign these exact bytes, before base64 encoding.",
            "content": {"application/octet-stream": {"schema": {
                "type": "string", "format": "binary",
            }}},
        },
        "parameters": [
            {"name": "X-Primer-Signature", "in": "header", "required": False,
             "description": "sha256=<hex> HMAC over \"<X-Primer-Timestamp>\\n<raw body>\" when the timestamp header is present, over the raw body otherwise; required when this trigger has an HMAC secret.",
             "schema": {"type": "string"}},
            {"name": "X-Primer-Timestamp", "in": "header", "required": False,
             "description": "Unix seconds inside the signature's 5 minute window; recommended (exact replays inside the window are refused).",
             "schema": {"type": "string"}},
            {"name": "query", "in": "query", "required": False,
             "style": "form", "explode": True,
             "description": "Webhook query parameters, delivered as webhook_query. Each property becomes a query parameter.",
             "schema": {"type": "object", "additionalProperties": {"type": "string"}}},
        ],
    },
    include_in_schema=True,
)
@route_tool(scoped_id="webhooks__receive_webhook", argument_overrides={
    "header.X-Primer-Signature": "signature",
    "header.X-Primer-Timestamp": "timestamp",
})
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

    # Body size cap, checked BEFORE the read (review 4.4): a declared
    # Content-Length over the cap is refused without reading a byte, and a
    # streamed body is read only until it crosses the cap.
    body = await _read_body_capped(request, _BODY_LIMIT_BYTES)

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

    # Signature verification: the timestamped scheme covers "<ts>\n<body>"
    # and is replay-protected; the pre-timestamp body-only scheme is
    # accepted only while the trigger's ``legacy_body_hmac`` is True (stored
    # rows that predate the flag; new triggers default to False).
    hmac_secret = trigger.config.hmac_secret
    if hmac_secret is not None:
        sig_header = request.headers.get("x-primer-signature")
        ts_header = request.headers.get("x-primer-timestamp")
        ok, code = _verify_webhook_signature(
            hmac_secret.get_secret_value(), body, sig_header, ts_header,
            trigger_id=trigger.id,
            legacy_body_only=trigger.config.legacy_body_hmac,
        )
        if not ok:
            raise HTTPException(
                status_code=401,
                detail={"code": code, "message": _SIGNATURE_REFUSAL_MESSAGES[code]},
            )
        # Cross-replica replay guard: the check above is per process (its
        # in-memory cache); the shared-store conditional insert is what a
        # second replica already accepted.
        if ts_header is not None and not await _claim_signature(
            sp, trigger.id, hmac_secret.get_secret_value(), body, ts_header,
        ):
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "replayed",
                    "message": _SIGNATURE_REFUSAL_MESSAGES["replayed"],
                },
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
                on_fired=lambda result: _record_fire(sp, row_id, result.fire_id),
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
@route_tool(scoped_id="webhooks__get_webhook_delivery")
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
