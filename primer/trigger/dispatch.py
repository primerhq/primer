"""fire_trigger orchestrator — Spec §6.

Single entry point for ALL trigger fires regardless of source (time-based
via the claim engine OR event-based via channel listener).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from primer.model.channel_event import ChannelEvent
from primer.model.event_matcher import matches
from primer.model.storage import Op, OffsetPage
from primer.model.trigger import Subscription, Trigger
from primer.storage.q import Q
from primer.trigger.fire_id import make_fire_id
from primer.trigger.payload import PayloadTemplateError, render_payload
from primer.trigger.sources import get_source
from primer.trigger.subscribers import DispatchDeps, get_dispatcher

# Import the dispatcher modules so their register() calls run at
# import time. Without these imports, ``get_dispatcher`` raises KeyError
# for every kind because nothing else in this module's import chain
# pulls the dispatcher implementations.
from primer.trigger.subscribers import agent_fresh_session as _afs  # noqa: F401
from primer.trigger.subscribers import graph_fresh_session as _gfs  # noqa: F401
from primer.trigger.subscribers import parked_session as _ps  # noqa: F401
from primer.trigger.subscribers import session_append as _sa  # noqa: F401


logger = logging.getLogger(__name__)


@dataclass
class FireResult:
    """Return shape of :func:`fire_trigger`.

    ``skipped`` is True when the trigger row was missing or disabled and
    no dispatch happened. ``fire_id`` is the deterministic correlation
    token for the fire; ``results`` is one envelope per attempted
    subscription dispatch (matching the dispatcher's structured result
    shape, with ``subscription_id`` appended).
    """

    skipped: bool = False
    fire_id: str | None = None
    results: list[dict] = field(default_factory=list)


_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://\S+")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+\S+")
_MAX_ERROR_MESSAGE = 300


def _safe_error_message(message: object) -> str | None:
    """Scrub a dispatcher error message before it leaves the process.

    The text is the ``str()`` of whatever a dispatcher caught, so it can
    embed a URL (webhook, DSN, SDK endpoint) carrying credentials in its
    userinfo or query string. Payload redaction is key-name based and
    never inspects string values, so this scrubs the value itself:
    anything URL-shaped becomes ``<url>``, bearer tokens are masked, and
    the result is bounded.
    """
    if not message:
        return None
    text = _BEARER_RE.sub("Bearer <redacted>", _URL_RE.sub("<url>", str(message)))
    if len(text) > _MAX_ERROR_MESSAGE:
        text = text[:_MAX_ERROR_MESSAGE] + "..."
    return text


async def _record_delivery_outcomes(
    *,
    deps: DispatchDeps,
    trigger: Trigger,
    fire_id: str,
    fired_at: datetime,
    scheduled_for: datetime | None,
    results: list[dict],
) -> None:
    """Persist what each subscription's delivery actually did.

    Two destinations, for two different questions:

    * the Subscription row (``last_fired_at`` / ``last_fire_error``) is
      LATEST-state: it answers "what happened last time this
      subscription was delivered to". A later clean fire clears it, on
      purpose - that is what a field named ``last_*`` means.
    * a ``trigger.delivery_failed`` event per failed delivery is the
      HISTORY: it is the only record that survives the next fire, and so
      the only answer to "did the 03:00 delivery ever arrive" after a
      catchup replay has fired the same trigger many times over.

    Skipped results (no event match, session busy/missing) were never
    attempted, so they touch neither. Best-effort by construction: this
    is bookkeeping about a fire that already happened, so a storage
    hiccup is logged and must not fail the fire it is reporting on.
    """
    from primer.events.recorder import recorder_for

    subs_storage = deps.storage_provider.get_storage(Subscription)
    recorder = recorder_for(deps.storage_provider)
    scheduled_iso = scheduled_for.isoformat() if scheduled_for else None
    for r in results:
        sub_id = r.get("subscription_id")
        if sub_id is None or r.get("skipped"):
            continue
        ok = bool(r.get("ok"))
        code = r.get("error_code")
        message = None if ok else _safe_error_message(r.get("error_message"))
        if not ok:
            await recorder.emit(
                "trigger.delivery_failed",
                actor=f"trigger:{trigger.id}",
                entity_kind="trigger",
                entity_id=trigger.id,
                payload={
                    "fire_id": fire_id,
                    "subscription_id": sub_id,
                    "scheduled_for": scheduled_iso,
                    "error_code": code,
                    "error_message": message,
                },
            )
        try:
            # Re-read: the row in hand predates the (possibly slow)
            # dispatch, and a whole-row write of it would clobber an
            # edit made meanwhile. Only the two outcome fields change.
            fresh = await subs_storage.get(sub_id)
            if fresh is None:
                continue
            await subs_storage.update(fresh.model_copy(update={
                "last_fired_at": fired_at,
                "last_fire_error": None if ok else json.dumps({
                    "code": code,
                    "message": message,
                    "fire_id": fire_id,
                    "scheduled_for": scheduled_iso,
                }),
            }))
        except Exception:  # noqa: BLE001 - bookkeeping, never fail the fire
            logger.exception(
                "trigger %s: could not record the delivery outcome on "
                "subscription %s", trigger.id, sub_id,
            )


async def fire_trigger(
    *,
    trigger_id: str,
    scheduled_for: datetime | None,
    deps: DispatchDeps,
    extra_context: dict | None = None,
) -> FireResult:
    """Fire a single trigger: load enabled subs, dispatch each.

    Per-subscription failures are isolated — a dispatcher raising or
    returning ``ok=False`` does not block sibling subs from running.
    The trigger row's ``last_fired_at`` is bumped to ``fired_at`` and
    ``last_fire_error`` is set to a JSON blob describing the first
    failure (if any) or cleared on success.

    ``extra_context`` is merged into the fire_context AFTER the source
    builds its base dict. This lets the webhook inbound endpoint inject
    ``webhook_body``, ``webhook_headers``, ``webhook_query``, and
    ``webhook_method`` without the source needing request-level coupling.
    """
    triggers_storage = deps.storage_provider.get_storage(Trigger)
    trigger = await triggers_storage.get(trigger_id)
    if trigger is None or not trigger.enabled:
        return FireResult(skipped=True)

    fired_at = datetime.now(timezone.utc)
    source = get_source(trigger.config.kind)
    fire_context = source.build_fire_context(
        trigger, fired_at=fired_at, scheduled_for=scheduled_for,
    )
    if extra_context:
        fire_context.update(extra_context)
    # fire_id is keyed on the LOGICAL fire instant (the scheduled tick
    # when present) so an at-least-once redelivery of the same tick
    # resolves to the same token and is deduped below. One-off / event
    # fires have no logical tick and fall back to wall-clock fired_at.
    fire_id = make_fire_id(trigger.id, scheduled_for or fired_at)
    fire_context["fire_id"] = fire_id

    # Idempotency gate: if this exact fire_id already dispatched, treat
    # the redelivery as a no-op. Per-trigger serialization is provided
    # by the claim engine (one TRIGGER claim per entity_id at a time),
    # so redeliveries arrive sequentially. The marker (last_fired_id) is
    # recorded AFTER the whole fan-out, at the end of this function, so
    # the gate only dedups the redelivery of a fire that FINISHED. A fire
    # that dies part way through leaves no marker, and its redelivery
    # dispatches every subscription again, including those the first
    # pass already served: at-least-once, not exactly-once (the
    # dispatchers do not yet look for an existing artefact by fire_id;
    # tests/trigger/test_fire_idempotency.py pins this). Recording the
    # marker before the dispatch would lose the unserved subscriptions
    # on a crash instead. It does not guard two truly-concurrent fires
    # of the same tick from distinct workers (the claim engine prevents
    # that upstream), and fire_now has no logical tick, so its fire_id
    # comes from the wall clock (milliseconds): a retried fire_now is a
    # new fire, not a redelivery, and is dispatched to every subscription.
    if trigger.last_fired_id == fire_id:
        logger.info(
            "trigger %s: duplicate fire_id %s; skipping (already dispatched)",
            trigger.id, fire_id,
        )
        return FireResult(skipped=True, fire_id=fire_id)

    from primer.events.recorder import recorder_for

    await recorder_for(deps.storage_provider).emit(
        "trigger.fired",
        actor=f"trigger:{trigger.id}",
        entity_kind="trigger",
        entity_id=trigger.id,
        payload={
            "fire_id": fire_id,
            "kind": str(trigger.config.kind),
            "scheduled_for": (
                scheduled_for.isoformat() if scheduled_for else None
            ),
        },
    )

    subs_storage = deps.storage_provider.get_storage(Subscription)
    q = Q(Subscription).where_op("trigger_id", Op.EQ, trigger.id)
    # Page in batches of 200 (OffsetPage max) to capture every sub
    # bound to this trigger. Real-world fan-out is small (<10 subs per
    # trigger), but bound the loop defensively at 10k to avoid an
    # infinite walk if the storage layer ever misbehaves.
    enabled_subs: list[Subscription] = []
    offset = 0
    while offset < 10_000:
        subs_page = await subs_storage.find(
            q.build(), OffsetPage(offset=offset, length=200),
        )
        enabled_subs.extend(s for s in subs_page.items if s.enabled)
        if len(subs_page.items) < 200:
            break
        offset += 200

    results: list[dict] = []
    for sub in enabled_subs:
        # Channel-event predicate: a sub with an event_matcher only fires when
        # the inbound ChannelEvent (carried in fire_context["event"]) matches.
        # A None matcher preserves today's time/webhook behavior (always fires).
        # A non-matching sub records an ok=True, skipped=True result so it is
        # visible but non-failing and isolated per-sub.
        if sub.event_matcher is not None:
            raw_event = fire_context.get("event")
            event = (
                ChannelEvent.model_validate(raw_event)
                if raw_event is not None
                else None
            )
            if event is None or not matches(sub.event_matcher, event):
                results.append({
                    "subscription_id": sub.id,
                    "ok": True,
                    "skipped": True,
                    "error_code": "skipped_no_match",
                    "error_message": "event_matcher did not match",
                })
                continue
        try:
            rendered = render_payload(sub.payload_template, fire_context)
        except PayloadTemplateError as exc:
            results.append({
                "subscription_id": sub.id,
                "ok": False,
                "skipped": False,
                "error_code": "payload_template_failed",
                "error_message": str(exc),
            })
            continue
        try:
            dispatcher = get_dispatcher(sub.config.kind)
            res = await dispatcher.dispatch(
                sub,
                rendered_payload=rendered,
                fire_context=fire_context,
                fire_id=fire_id,
                deps=deps,
            )
            results.append({"subscription_id": sub.id, **res.model_dump()})
        except Exception as exc:  # noqa: BLE001 — isolate per-sub failures
            logger.exception("dispatcher error for sub %s", sub.id)
            results.append({
                "subscription_id": sub.id,
                "ok": False,
                "skipped": False,
                "error_code": "dispatch_failed",
                "error_message": str(exc),
            })

    await _record_delivery_outcomes(
        deps=deps, trigger=trigger, fire_id=fire_id, fired_at=fired_at,
        scheduled_for=scheduled_for, results=results,
    )

    # Update trigger row's last_fired_at + last_fired_id + error. Recording
    # last_fired_id here is the dedup marker the gate above reads on a
    # redelivery.
    trigger.last_fired_at = fired_at
    trigger.last_fired_id = fire_id
    first_err = next((r for r in results if not r.get("ok")), None)
    if first_err:
        trigger.last_fire_error = json.dumps({
            "code": first_err.get("error_code"),
            "subscription_id": first_err.get("subscription_id"),
            "message": first_err.get("error_message"),
        })
    else:
        trigger.last_fire_error = None
    await triggers_storage.update(trigger)
    return FireResult(skipped=False, fire_id=fire_id, results=results)


__all__ = ["fire_trigger", "FireResult"]
