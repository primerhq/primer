"""Trigger CRUD + fire orchestration service — Spec §12.2.

The shared service layer is the single source of truth for
trigger/subscription mutations. Both the REST router (``primer.api.routers.triggers``)
and the management toolset (``primer.toolset.trigger``) call into it,
so behaviour stays consistent across surfaces.

Typed exceptions are mapped to HTTP error envelopes by the router.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from primer.model.storage import OffsetPage, Op
from primer.model.trigger import (
    Subscription,
    Trigger,
    WebhookTriggerConfig,
)
from primer.storage.q import Q
from primer.trigger.cron import (
    CronInvalid,
    TimezoneInvalid,
    validate_cron,
    validate_timezone,
)
from primer.trigger.sources import get_source


# ---------------------------------------------------------------------------
# Typed exceptions — router maps each to an HTTP envelope (code string)
# ---------------------------------------------------------------------------


class TriggerNotFound(Exception):
    """Trigger row missing for the given id."""


class SubscriptionNotFound(Exception):
    """Subscription row missing or not bound to the supplied trigger."""


class TriggerKindImmutable(Exception):
    """An update attempted to change the trigger's ``config.kind`` discriminator."""


class ParkedSessionOnlyFromYield(Exception):
    """A subscription with ``kind='parked_session'`` was supplied through the
    public create path. Those are only created via the ``subscribe_to_trigger``
    yielding tool."""


class TriggerSlugConflict(Exception):
    """A trigger with the requested slug already exists."""


class WebhookTokenNotFound(Exception):
    """No trigger found for the supplied webhook token."""


# ---------------------------------------------------------------------------
# Deps
# ---------------------------------------------------------------------------


@dataclass
class ServiceDeps:
    """Bundle passed through to every service function.

    ``claim_engine`` and ``event_bus`` may be None — the service then
    treats lease upserts / bus pulses as no-ops. The REST surface in
    tests does not wire a claim engine; production lifespan does.
    """

    storage_provider: Any
    claim_engine: Any | None = None
    event_bus: Any | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _mint_webhook_token() -> str:
    """Return a 32-hex-char cryptographically random token."""
    import secrets
    return secrets.token_hex(16)  # 16 bytes = 32 hex chars


def _validate_config(cfg) -> None:
    """Raise :class:`CronInvalid` / :class:`TimezoneInvalid` for scheduled configs.

    Delayed configs need no further validation beyond Pydantic — ``fire_at``
    is a datetime by type.
    """
    if cfg.kind == "scheduled":
        validate_cron(cfg.cron)
        validate_timezone(cfg.timezone)


async def _upsert_claim_if_eligible(
    trigger: Trigger, *, deps: ServiceDeps,
) -> None:
    """Best-effort claim lease upsert.

    No-op when ``deps.claim_engine`` is None, the trigger is disabled,
    its source is not eligible for claim-driven fires, or
    ``next_fire_at`` is None.
    """
    if deps.claim_engine is None:
        return
    if not trigger.enabled or trigger.next_fire_at is None:
        return
    source = get_source(trigger.config.kind)
    if not getattr(source, "eligible_for_claim", False):
        return
    from primer.int.claim import ClaimKind

    try:
        await deps.claim_engine.upsert(
            ClaimKind.TRIGGER,
            trigger.id,
            priority=10,
            next_attempt_at=trigger.next_fire_at,
        )
    except TypeError:
        # Older engine signatures don't accept next_attempt_at kwarg —
        # fall back to the basic upsert (still correct; the next_fire_at
        # is also persisted on the trigger row itself).
        await deps.claim_engine.upsert(
            ClaimKind.TRIGGER, trigger.id, priority=10,
        )


# ---------------------------------------------------------------------------
# Trigger CRUD
# ---------------------------------------------------------------------------


async def create_trigger(
    *,
    slug: str,
    name: str,
    description: str | None,
    config,
    enabled: bool = True,
    deps: ServiceDeps,
) -> Trigger:
    """Create a new Trigger row.

    Slug uniqueness is enforced via a pre-write find(); the storage
    layer's create() also raises ConflictError on id clash but that
    only protects the surrogate ``id``, not the human slug.
    """
    storage = deps.storage_provider.get_storage(Trigger)
    # Slug uniqueness
    q = Q(Trigger).where_op("slug", Op.EQ, slug)
    page = await storage.find(q.build(), OffsetPage(offset=0, length=1))
    if page.items:
        raise TriggerSlugConflict(f"slug {slug!r} already in use")
    # Webhook triggers always get a server-minted token regardless of what
    # the caller supplied. This ensures the token is cryptographically random
    # and prevents callers from choosing predictable tokens.
    if config.kind == "webhook":
        config = WebhookTriggerConfig(
            token=_mint_webhook_token(),
            hmac_secret=config.hmac_secret,
        )
    _validate_config(config)

    source = get_source(config.kind)
    # Build a transient trigger to compute the initial next_fire_at —
    # sources operate against a Trigger instance, not the raw config.
    tmp_trigger = Trigger(
        id="tmp-for-compute",
        slug=slug,
        name=name,
        description=description,
        config=config,
        enabled=enabled,
        next_fire_at=None,
        created_at=_now(),
    )
    nxt = source.compute_next_fire_at(tmp_trigger, now=_now()) if enabled else None

    trigger = Trigger(
        id=f"tr-{uuid.uuid4().hex[:12]}",
        slug=slug,
        name=name,
        description=description,
        config=config,
        enabled=enabled,
        next_fire_at=nxt,
        created_at=_now(),
    )
    await storage.create(trigger)
    await _upsert_claim_if_eligible(trigger, deps=deps)
    return trigger


async def update_trigger(
    *,
    trigger_id: str,
    name: str | None = None,
    description: str | None = None,
    enabled: bool | None = None,
    config=None,
    deps: ServiceDeps,
) -> Trigger:
    """Partial update.

    Changing the trigger's ``config.kind`` discriminator is rejected
    with :class:`TriggerKindImmutable` (delete + recreate is the
    operator path for kind changes).
    """
    storage = deps.storage_provider.get_storage(Trigger)
    trigger = await storage.get(trigger_id)
    if trigger is None:
        raise TriggerNotFound(trigger_id)
    if config is not None and config.kind != trigger.config.kind:
        raise TriggerKindImmutable(
            f"cannot change kind from {trigger.config.kind!r} to {config.kind!r}"
        )
    if name is not None:
        trigger.name = name
    if description is not None:
        trigger.description = description
    if enabled is not None:
        trigger.enabled = enabled
    if config is not None:
        _validate_config(config)
        # For webhook triggers, preserve the existing token unless the
        # caller has supplied a non-empty one (rotate path uses rotate_webhook_token
        # explicitly; update is only used for hmac_secret set/clear).
        if config.kind == "webhook" and not config.token:
            config = WebhookTriggerConfig(
                token=trigger.config.token,
                hmac_secret=config.hmac_secret,
            )
        trigger.config = config

    source = get_source(trigger.config.kind)
    trigger.next_fire_at = (
        source.compute_next_fire_at(trigger, now=_now())
        if trigger.enabled else None
    )
    await storage.update(trigger)
    await _upsert_claim_if_eligible(trigger, deps=deps)
    return trigger


async def delete_trigger(*, trigger_id: str, deps: ServiceDeps) -> None:
    """Delete a trigger and cascade-delete its subscriptions."""
    storage = deps.storage_provider.get_storage(Trigger)
    subs_storage = deps.storage_provider.get_storage(Subscription)
    trigger = await storage.get(trigger_id)
    if trigger is None:
        raise TriggerNotFound(trigger_id)
    # Cascade-delete all subscriptions bound to this trigger.
    q = Q(Subscription).where_op("trigger_id", Op.EQ, trigger_id)
    offset = 0
    while offset < 10_000:
        page = await subs_storage.find(
            q.build(), OffsetPage(offset=offset, length=200),
        )
        for sub in page.items:
            try:
                await subs_storage.delete(sub.id)
            except Exception:
                # Already-gone is fine; keep cascading.
                pass
        if len(page.items) < 200:
            break
        offset += 200
    await storage.delete(trigger_id)


async def fire_now(*, trigger_id: str, deps: ServiceDeps):
    """Synchronously fire a trigger and return its :class:`FireResult`.

    Delegates to :func:`primer.trigger.dispatch.fire_trigger`. Raises
    :class:`TriggerNotFound` if the trigger row is missing; otherwise
    returns whatever ``fire_trigger`` reports (including the
    skipped=True envelope when the trigger is disabled).
    """
    storage = deps.storage_provider.get_storage(Trigger)
    trigger = await storage.get(trigger_id)
    if trigger is None:
        raise TriggerNotFound(trigger_id)

    from primer.trigger.dispatch import fire_trigger
    from primer.trigger.subscribers import DispatchDeps

    dispatch_deps = DispatchDeps(
        storage_provider=deps.storage_provider,
        claim_engine=deps.claim_engine,
        scheduler=None,
        event_bus=deps.event_bus,
    )
    return await fire_trigger(
        trigger_id=trigger_id,
        scheduled_for=None,
        deps=dispatch_deps,
    )


# ---------------------------------------------------------------------------
# Subscription CRUD
# ---------------------------------------------------------------------------


async def create_subscription(
    *,
    trigger_id: str,
    config,
    payload_template: str | None = None,
    parallelism: str = "skip",
    description: str | None = None,
    enabled: bool = True,
    event_matcher=None,
    reply_target=None,
    deps: ServiceDeps,
) -> Subscription:
    """Create a subscription bound to ``trigger_id``.

    Subscriptions with ``kind='parked_session'`` are reserved for the
    ``subscribe_to_trigger`` yielding tool — the REST + toolset create
    paths reject them.
    """
    triggers_storage = deps.storage_provider.get_storage(Trigger)
    trigger = await triggers_storage.get(trigger_id)
    if trigger is None:
        raise TriggerNotFound(trigger_id)
    if config.kind == "parked_session":
        raise ParkedSessionOnlyFromYield(
            "subscriptions of kind 'parked_session' are only created via "
            "the subscribe_to_trigger yielding tool"
        )
    subs_storage = deps.storage_provider.get_storage(Subscription)
    sub = Subscription(
        id=f"sb-{uuid.uuid4().hex[:12]}",
        trigger_id=trigger_id,
        config=config,
        payload_template=payload_template,
        parallelism=parallelism,
        enabled=enabled,
        description=description,
        event_matcher=event_matcher,
        reply_target=reply_target,
        created_at=_now(),
    )
    await subs_storage.create(sub)
    return sub


# Sentinel used so callers can omit optional nullable fields without
# accidentally clearing them.
_UNSET: Any = object()


async def update_subscription(
    *,
    trigger_id: str,
    subscription_id: str,
    payload_template: Any = _UNSET,
    parallelism: str | None = None,
    enabled: bool | None = None,
    description: Any = _UNSET,
    event_matcher: Any = _UNSET,
    reply_target: Any = _UNSET,
    deps: ServiceDeps,
) -> Subscription:
    """Partial update.

    Nullable string fields (``payload_template``, ``description``) use
    the ``_UNSET`` sentinel so callers can leave them untouched while
    still being able to explicitly clear them by passing ``None``.
    """
    subs_storage = deps.storage_provider.get_storage(Subscription)
    sub = await subs_storage.get(subscription_id)
    if sub is None or sub.trigger_id != trigger_id:
        raise SubscriptionNotFound(subscription_id)
    if payload_template is not _UNSET:
        sub.payload_template = payload_template
    if parallelism is not None:
        sub.parallelism = parallelism
    if enabled is not None:
        sub.enabled = enabled
    if description is not _UNSET:
        sub.description = description
    if event_matcher is not _UNSET:
        sub.event_matcher = event_matcher
    if reply_target is not _UNSET:
        sub.reply_target = reply_target
    await subs_storage.update(sub)
    return sub


async def delete_subscription(
    *, trigger_id: str, subscription_id: str, deps: ServiceDeps,
) -> None:
    subs_storage = deps.storage_provider.get_storage(Subscription)
    sub = await subs_storage.get(subscription_id)
    if sub is None or sub.trigger_id != trigger_id:
        raise SubscriptionNotFound(subscription_id)
    await subs_storage.delete(subscription_id)


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


async def get_trigger(*, trigger_id: str, deps: ServiceDeps) -> Trigger:
    storage = deps.storage_provider.get_storage(Trigger)
    trigger = await storage.get(trigger_id)
    if trigger is None:
        raise TriggerNotFound(trigger_id)
    return trigger


async def get_trigger_by_webhook_token(
    *, token: str, deps: ServiceDeps,
) -> Trigger:
    """Resolve a webhook trigger by its capability token.

    Raises :class:`WebhookTokenNotFound` when no trigger matches.
    """
    storage = deps.storage_provider.get_storage(Trigger)
    q = Q(Trigger).where_op("config.token", Op.EQ, token)
    page = await storage.find(q.build(), OffsetPage(offset=0, length=1))
    if not page.items:
        raise WebhookTokenNotFound(token)
    return page.items[0]


async def rotate_webhook_token(
    *, trigger_id: str, deps: ServiceDeps,
) -> Trigger:
    """Rotate the capability token of a webhook trigger.

    Returns the updated trigger with the new token.
    Raises :class:`TriggerNotFound` if missing, or ``ValueError`` if the
    trigger is not of kind='webhook'.
    """
    storage = deps.storage_provider.get_storage(Trigger)
    trigger = await storage.get(trigger_id)
    if trigger is None:
        raise TriggerNotFound(trigger_id)
    if trigger.config.kind != "webhook":
        raise ValueError(
            f"rotate_webhook_token requires kind='webhook', got {trigger.config.kind!r}"
        )
    new_token = _mint_webhook_token()
    trigger.config = WebhookTriggerConfig(
        token=new_token,
        hmac_secret=trigger.config.hmac_secret,
    )
    await storage.update(trigger)
    return trigger


async def get_subscription(
    *, trigger_id: str, subscription_id: str, deps: ServiceDeps,
) -> Subscription:
    subs_storage = deps.storage_provider.get_storage(Subscription)
    sub = await subs_storage.get(subscription_id)
    if sub is None or sub.trigger_id != trigger_id:
        raise SubscriptionNotFound(subscription_id)
    return sub


# Window size for the paginated list helpers below. Each list walks the
# storage backend page by page until exhausted so NOTHING is silently
# dropped past a single fixed cap (the previous fixed ``length=200`` lost
# every row beyond the 200th). Memory stays bounded to one window at a
# time while accumulating the full result.
_LIST_PAGE_SIZE = 200


async def _drain_list(storage, *, predicate=None) -> list:
    """Page through ``storage`` (``find`` with ``predicate``, or ``list`` when
    ``predicate`` is None) until exhausted, returning every row.

    Bounded memory: only one window of ``_LIST_PAGE_SIZE`` rows is held by
    the backend per round-trip; the accumulator grows with the true result
    size, which is the intended (un-truncated) behaviour."""
    out: list = []
    offset = 0
    while True:
        page = (
            await storage.list(OffsetPage(offset=offset, length=_LIST_PAGE_SIZE))
            if predicate is None
            else await storage.find(
                predicate, OffsetPage(offset=offset, length=_LIST_PAGE_SIZE)
            )
        )
        out.extend(page.items)
        if len(page.items) < _LIST_PAGE_SIZE:
            break
        offset += _LIST_PAGE_SIZE
    return out


async def list_triggers(
    *,
    kind: str | None = None,
    enabled: bool | None = None,
    deps: ServiceDeps,
) -> list[Trigger]:
    """Return ALL triggers (optionally filtered), paging through every row.

    Previously capped at the first 200 rows, silently dropping the rest;
    now walks the backend page by page until exhausted."""
    storage = deps.storage_provider.get_storage(Trigger)
    if kind is None and enabled is None:
        return await _drain_list(storage)
    q = Q(Trigger)
    if kind is not None:
        q = q.where_op("config.kind", Op.EQ, kind)
    if enabled is not None:
        q = q.where_op("enabled", Op.EQ, enabled)
    return await _drain_list(storage, predicate=q.build())


async def list_subscriptions(
    *, trigger_id: str, deps: ServiceDeps,
) -> list[Subscription]:
    """Return ALL subscriptions for a trigger, paging through every row
    (previously capped at the first 200)."""
    subs_storage = deps.storage_provider.get_storage(Subscription)
    q = Q(Subscription).where_op("trigger_id", Op.EQ, trigger_id)
    return await _drain_list(subs_storage, predicate=q.build())


__all__ = [
    "ParkedSessionOnlyFromYield",
    "ServiceDeps",
    "SubscriptionNotFound",
    "TriggerKindImmutable",
    "TriggerNotFound",
    "TriggerSlugConflict",
    "WebhookTokenNotFound",
    "create_subscription",
    "create_trigger",
    "delete_subscription",
    "delete_trigger",
    "fire_now",
    "get_subscription",
    "get_trigger",
    "get_trigger_by_webhook_token",
    "list_subscriptions",
    "list_triggers",
    "rotate_webhook_token",
    "update_subscription",
    "update_trigger",
]
