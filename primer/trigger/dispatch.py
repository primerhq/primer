"""fire_trigger orchestrator — Spec §6.

Single entry point for ALL trigger fires regardless of source (time-based
via the claim engine OR event-based via channel listener).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from primer.model.storage import Op, OffsetPage
from primer.model.trigger import Subscription, Trigger
from primer.storage.q import Q
from primer.trigger.fire_id import make_fire_id
from primer.trigger.payload import PayloadTemplateError, render_payload
from primer.trigger.sources import get_source
from primer.trigger.subscribers import DispatchDeps, get_dispatcher

# Import the four dispatcher modules so their register() calls run at
# import time. Without these imports, ``get_dispatcher`` raises KeyError
# for every kind because nothing else in this module's import chain
# pulls the dispatcher implementations.
from primer.trigger.subscribers import chat_message as _cm  # noqa: F401
from primer.trigger.subscribers import agent_fresh_session as _afs  # noqa: F401
from primer.trigger.subscribers import graph_fresh_session as _gfs  # noqa: F401
from primer.trigger.subscribers import parked_session as _ps  # noqa: F401


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


async def fire_trigger(
    *,
    trigger_id: str,
    scheduled_for: datetime | None,
    deps: DispatchDeps,
) -> FireResult:
    """Fire a single trigger: load enabled subs, dispatch each.

    Per-subscription failures are isolated — a dispatcher raising or
    returning ``ok=False`` does not block sibling subs from running.
    The trigger row's ``last_fired_at`` is bumped to ``fired_at`` and
    ``last_fire_error`` is set to a JSON blob describing the first
    failure (if any) or cleared on success.
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
    # fire_id is keyed on the LOGICAL fire instant (the scheduled tick
    # when present) so an at-least-once redelivery of the same tick
    # resolves to the same token and is deduped below. One-off / event
    # fires have no logical tick and fall back to wall-clock fired_at.
    fire_id = make_fire_id(trigger.id, scheduled_for or fired_at)
    fire_context["fire_id"] = fire_id

    # Idempotency gate: if this exact fire_id already dispatched, treat
    # the redelivery as a no-op. Per-trigger serialization is provided
    # by the claim engine (one TRIGGER claim per entity_id at a time),
    # so redeliveries arrive sequentially; recording last_fired_id on
    # the row before dispatch makes the second pass a logged skip. This
    # closes the sequential-redelivery window; it does not guard two
    # truly-concurrent fires of the same tick from distinct workers
    # (the claim engine prevents that upstream).
    if trigger.last_fired_id == fire_id:
        logger.info(
            "trigger %s: duplicate fire_id %s; skipping (already dispatched)",
            trigger.id, fire_id,
        )
        return FireResult(skipped=True, fire_id=fire_id)

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
