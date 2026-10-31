"""Trigger CRUD + fire orchestration service — Spec §12.2.

The shared service layer is the single source of truth for
trigger/subscription mutations. Both the REST router (``primer.api.routers.triggers``)
and the management toolset (``primer.toolset.trigger``) call into it,
so behaviour stays consistent across surfaces.

Typed exceptions are mapped to HTTP error envelopes by the router.

Access (access-control plan P3 Task 4, Decision 3): a trigger is owned and shareable, and its subscriptions inherit it. Every
function that takes ``person`` enforces the level on the trigger: read sees it and its subscriptions, full edits it, fires it
and manages its subscriptions, owner deletes it, rotates its webhook token and shares it. A trigger the person holds no level
on raises :class:`TriggerNotFound` (exactly as an unknown id); too low a level raises
:class:`primer.model.except_.AccessDeniedError`. ``person=None`` is an internal call (the platform: the dispatcher, the
delete cascade), which is not gated; every REST route and tool passes the caller. Who a fire runs as is unchanged: the
trigger's ``owner`` (its last saver) and each subscription's owner (:mod:`primer.trigger.owner`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from primer.access.person import Person
from primer.access.policy import require, visible_predicate
from primer.access.service import update_keeping_access
from primer.model.access import Access, AccessLevel
from primer.model.event_matcher import validate_text_pattern
from primer.model.except_ import NotFoundError
from primer.model.principal import PrincipalRef
from primer.model.storage import OffsetPage, Op, Predicate
from primer.model.trigger import (
    PARALLELISM_VALUES,
    ExplicitReplyTarget,
    Parallelism,
    Subscription,
    Trigger,
    refuse_retired_reply_target,
)
from primer.storage.q import Q
from primer.trigger.reach import (
    refuse_target_out_of_access,
    subscription_in_reach,
    subscription_reaches_outside,
)
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


class SubscriptionTargetNotFound(Exception):
    """A subscription saved from a workspace session names a target outside that session's reach, or one that does not exist: answered as a
    ``not-found`` in the words of a target that does not exist (:func:`primer.trigger.reach.refuse_target_out_of_access`). Nothing is stored."""


class ReplyTargetChannelNotFound(Exception):
    """An explicit reply_target ``{channel_id, anchor}`` names a channel that does not exist (round 2 of #932, item 3): refused the same way
    ``set_reply_binding`` refuses an unknown channel, nothing is stored."""


class ReplyTargetAnchorInvalid(Exception):
    """An explicit reply_target's anchor is not a thread id the channel's platform can post into (a Discord anchor must be a numeric id,
    round 2 of #932, item 3). Nothing is stored."""


async def _validate_explicit_reply_target(reply_target: Any, deps: "ServiceDeps") -> None:
    """Validate an explicit ``{channel_id, anchor}`` reply target at save time (round 2 of #932, item 3).

    A relative literal names no channel, so it validates as is. An explicit target must name a stored channel (refused with
    :class:`ReplyTargetChannelNotFound`, a not-found the same way ``set_reply_binding`` refuses one), and a Discord anchor must be a
    numeric thread id (:class:`ReplyTargetAnchorInvalid`). A dict is coerced through the model first, so a malformed target is a
    validation error, not a channel lookup. ``None`` (no target) and a relative literal are returned as is.
    """
    if reply_target is None or isinstance(reply_target, str):
        return
    if not isinstance(reply_target, ExplicitReplyTarget):
        reply_target = ExplicitReplyTarget.model_validate(reply_target)
    from primer.model.channel import Channel, ChannelProviderType

    channel = await deps.storage_provider.get_storage(Channel).get(reply_target.channel_id)
    if channel is None:
        raise ReplyTargetChannelNotFound(f"reply_target: channel {reply_target.channel_id!r} does not exist")
    if channel.provider == ChannelProviderType.DISCORD and reply_target.anchor is not None and not reply_target.anchor.isdigit():
        raise ReplyTargetAnchorInvalid(
            f"reply_target: a Discord anchor must be a numeric thread id, not {reply_target.anchor!r}"
        )


#: How pydantic serialises a set ``SecretStr`` (``hmac_secret``) in a read; a PUT that echoes it keeps the stored secret.
_SECRET_MASK = "**********"


class WebhookTokenNotFound(Exception):
    """No trigger found for the supplied webhook token."""


# ---------------------------------------------------------------------------
# Deps
# ---------------------------------------------------------------------------


@dataclass
class ServiceDeps:
    """Bundle passed through to every service function.

    ``claim_engine`` and ``event_bus`` may be None — the service then
    treats lease upserts and deletions / bus pulses as no-ops. The REST surface in
    tests does not wire a claim engine; production lifespan does.

    ``scheduler`` and ``workspace_registry`` are only consumed by
    :func:`fire_now`, which threads them into the dispatch ``DispatchDeps``.
    The fresh-session dispatchers (``agent_fresh_session`` /
    ``graph_fresh_session``) need a live ``workspace_registry`` to allocate
    the on-disk session slot; without it a fired session is created but
    never runs. They are optional here so the CRUD service functions (which
    never fire) can keep building ``ServiceDeps`` without them.
    """

    storage_provider: Any
    claim_engine: Any | None = None
    event_bus: Any | None = None
    scheduler: Any | None = None
    workspace_registry: Any | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _require_trigger(trigger_id: str, person: Person | None, need: AccessLevel, deps: "ServiceDeps") -> Trigger:
    """The trigger row, after enforcing ``need`` for ``person`` (``None``: an internal call, not gated).

    No row, or no level, raises :class:`TriggerNotFound` (an out-of-access trigger answers exactly like an unknown id); too low
    a level raises :class:`AccessDeniedError` (403 ``access_denied``, tool ``forbidden``).
    """
    trigger = await deps.storage_provider.get_storage(Trigger).get(trigger_id)
    if trigger is None:
        raise TriggerNotFound(trigger_id)
    if person is not None:
        try:
            require(person, trigger.access, need, what=f"Trigger {trigger_id!r}")
        except NotFoundError:
            raise TriggerNotFound(trigger_id) from None
    return trigger


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
    from primer.int.claim import CLAIM_PRIORITY_OPERATOR, ClaimKind

    try:
        await deps.claim_engine.upsert(
            ClaimKind.TRIGGER,
            trigger.id,
            priority=CLAIM_PRIORITY_OPERATOR,
            next_attempt_at=trigger.next_fire_at,
        )
    except TypeError:
        # Older engine signatures don't accept next_attempt_at kwarg -
        # fall back to the basic upsert (still correct; the next_fire_at
        # is also persisted on the trigger row itself).
        await deps.claim_engine.upsert(
            ClaimKind.TRIGGER, trigger.id, priority=CLAIM_PRIORITY_OPERATOR,
        )


async def _delete_claim_lease(trigger_id: str, *, deps: ServiceDeps) -> None:
    """Claim lease deletion on the way out (ticket 01a16ede).

    No-op when ``deps.claim_engine`` is None; both engines answer a missing lease with a no-op, so it is safe to
    ask unconditionally. The claim loop reads the stored row of every armed lease on every poll: a disabled
    trigger's lease is ineligible forever, and a deleted trigger's lease outlives its row, so neither may keep
    paying that cost.
    """
    if deps.claim_engine is None:
        return
    from primer.int.claim import ClaimKind

    await deps.claim_engine.delete_lease(ClaimKind.TRIGGER, trigger_id)


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
    owner: PrincipalRef | None = None,
    person: Person | None = None,
    deps: ServiceDeps,
) -> Trigger:
    """Create a new Trigger row.

    ``person`` (the caller) becomes its sole owner (access); with no user id (an admin-managed or internal create) the row is
    admin-only. A body never chooses the access.

    ``owner`` is the caller (the REST actor or the calling run's
    ``initiated_by``); a fired run is ranked no higher than it (see
    :mod:`primer.trigger.owner`). None records no owner, which fires as an
    ordinary user.

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
    # the caller supplied. model_copy keeps every OTHER field (interactive,
    # wait_timeout_seconds, hmac_secret) instead of re-listing them, which is
    # what used to silently drop newly added config fields.
    if config.kind == "webhook":
        config = config.model_copy(update={"token": _mint_webhook_token()})
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
        owner=owner,
        access=Access.owned_by(person.user_id) if person is not None and person.user_id is not None else Access(),
    )
    await storage.create(trigger)
    await _upsert_claim_if_eligible(trigger, deps=deps)
    return trigger


async def _refuse_trigger_out_of_reach(trigger_id: str, reach: Person | None, deps: ServiceDeps) -> None:
    """Raise :class:`TriggerNotFound` when a subscription of the trigger starts or feeds a session outside ``reach`` (review of #874, B1).

    ``reach`` is the calling session's (:func:`primer.trigger.reach.caller_scope`); ``None`` (an admin or the system principal) refuses
    nothing. Asked right where the trigger is looked up, so the refusal is exactly what an unknown trigger answers.
    """
    if reach is None:
        return
    for sub in await list_subscriptions(trigger_id=trigger_id, deps=deps):
        if await subscription_reaches_outside(deps.storage_provider, reach, sub):
            raise TriggerNotFound(trigger_id)


async def _require_channel_read(sp: Any, config: Any, person: Person, *, as_target: bool = False) -> None:
    """READ on the channel a channel trigger fires from (:func:`primer.trigger.channel_access.require_channel_trigger_access`).

    An unreadable channel answers as a missing one (``NotFoundError``); ``as_target`` says it as a missing subscription target, the way a subscription's own
    unreadable target is refused.
    """
    from primer.trigger.channel_access import require_channel_trigger_access

    try:
        await require_channel_trigger_access(sp, person, config)
    except NotFoundError as exc:
        if as_target:
            raise SubscriptionTargetNotFound(str(exc)) from None
        raise


async def _refuse_credential_change_below_owner(trigger: Trigger, config, person: Person | None, deps: ServiceDeps) -> None:
    """A webhook trigger's token and HMAC secret are changed by an OWNER only, as the rotate is (access-control P3 SEC, finding 3).

    A full grantee may save the rest, but a body that carries a token different from the stored one (the mask, an empty one and the stored one echoed
    back all keep it), or an HMAC secret different from the stored one (the mask keeps it; none clears it), is refused. Otherwise a full grantee would choose
    the capability token of someone else's trigger. Admins hold owner, so they are unaffected. ``person`` None (an internal caller) is not checked.
    """
    if config.kind != "webhook" or trigger.config.kind != "webhook" or person is None:
        return
    from primer.trigger.owner import WEBHOOK_TOKEN_MASK

    token_changes = config.token not in ("", WEBHOOK_TOKEN_MASK, trigger.config.token)
    new_secret = config.hmac_secret.get_secret_value() if config.hmac_secret is not None else None
    old_secret = trigger.config.hmac_secret.get_secret_value() if trigger.config.hmac_secret is not None else None
    secret_changes = new_secret != _SECRET_MASK and new_secret != old_secret
    if token_changes or secret_changes:
        await _require_trigger(trigger.id, person, AccessLevel.OWNER, deps)


async def update_trigger(
    *,
    trigger_id: str,
    name: str | None = None,
    description: str | None = None,
    enabled: bool | None = None,
    config=None,
    owner: PrincipalRef | None = None,
    person: Person | None = None,
    reach: Person | None = None,
    deps: ServiceDeps,
) -> Trigger:
    """Partial update; needs full on the trigger (Decision 3).

    The caller becomes the owner: whoever last saved a trigger is who its
    runs are ranked by. ``owner=None`` (a call with no identity) clears it,
    so the trigger fires as an ordinary user.

    The level is asked after the reach check; a refusal changes nothing (the
    row keeps its owner, its timing and its subscriptions). A full grantee who
    saves becomes the run-as owner, as anyone who saves does: they act as
    themselves. When the save hands a webhook trigger to a different owner a
    new token is minted, so nobody who knew the old one rides along at the new
    owner's rank. A token equal to the mask (a body round-tripped from a masked
    read) keeps the stored one. The row is written with
    :func:`primer.access.service.update_keeping_access`: a sharing change made
    meanwhile answers ``ConflictError`` and is never undone.

    Changing the trigger's ``config.kind`` discriminator is rejected
    with :class:`TriggerKindImmutable` (delete + recreate is the
    operator path for kind changes).

    ``reach`` is the calling session's: a trigger with a subscription that
    starts or feeds a session outside it answers :class:`TriggerNotFound`
    (re-enabling or re-timing it would fire that subscription).

    The claim lease follows the enabled state (ticket 01a16ede): a save that
    leaves the trigger enabled re-arms or refreshes it (:func:`_upsert_claim_if_eligible`),
    and a save that disables it drops it (:func:`_delete_claim_lease`), so a
    disabled trigger is not read, and found ineligible, on every poll.
    """
    storage = deps.storage_provider.get_storage(Trigger)
    trigger = await _require_trigger(trigger_id, person, AccessLevel.FULL, deps)
    await _refuse_trigger_out_of_reach(trigger_id, reach, deps)
    if person is not None:
        # The saver becomes the run-as owner of the trigger as it will stand: a channel trigger needs READ on its channel even when this save names no config.
        await _require_channel_read(deps.storage_provider, config if config is not None else trigger.config, person)
    if config is not None and config.kind != trigger.config.kind:
        raise TriggerKindImmutable(
            f"cannot change kind from {trigger.config.kind!r} to {config.kind!r}"
        )
    from primer.trigger.owner import WEBHOOK_TOKEN_MASK, same_owner

    adopted = not await same_owner(trigger.owner, owner, deps.storage_provider)
    trigger.owner = owner
    if name is not None:
        trigger.name = name
    if description is not None:
        trigger.description = description
    if enabled is not None:
        trigger.enabled = enabled
    if config is not None:
        _validate_config(config)
        await _refuse_credential_change_below_owner(trigger, config, person, deps)
        # For webhook triggers, preserve the existing token unless the
        # caller has supplied a non-empty one (rotate path uses rotate_webhook_token
        # explicitly; update is only used for hmac_secret set/clear).
        # An empty token, the mask, or the stored token echoed back from a
        # read all mean "keep the stored one" -- and the stored one is what
        # the previous owner knows, so the change-of-owner re-mint below
        # still applies to it. Only a token that differs from the stored one
        # is caller-chosen (nobody else knew it).
        if config.kind == "webhook" and config.token in (
            "", WEBHOOK_TOKEN_MASK, trigger.config.token,
        ):
            config = config.model_copy(update={"token": trigger.config.token})
        elif config.kind == "webhook":
            adopted = False
        # The hmac_secret mask (a SecretStr serialised by a read) round-trips
        # to the stored secret, never to the literal mask.
        if (
            config.kind == "webhook"
            and config.hmac_secret is not None
            and config.hmac_secret.get_secret_value() == _SECRET_MASK
        ):
            config = config.model_copy(
                update={"hmac_secret": trigger.config.hmac_secret},
            )
        trigger.config = config
    if trigger.config.kind == "webhook" and adopted:
        trigger.config = trigger.config.model_copy(
            update={"token": _mint_webhook_token()},
        )

    source = get_source(trigger.config.kind)
    trigger.next_fire_at = (
        source.compute_next_fire_at(trigger, now=_now())
        if trigger.enabled else None
    )
    # Every field but the sharing, guarded on its rev (P3 Decision 8): a sharing change made meanwhile is a conflict, never undone.
    trigger = await update_keeping_access(storage, trigger, trigger, what=f"Trigger {trigger_id!r}")
    if trigger.enabled:
        await _upsert_claim_if_eligible(trigger, deps=deps)
    else:
        # A save that turns the trigger off drops its lease: it would be read, and found ineligible, on every poll (ticket 01a16ede).
        # A re-enable re-arms it via the upsert above.
        await _delete_claim_lease(trigger.id, deps=deps)
    return trigger


async def delete_trigger(
    *, trigger_id: str, person: Person | None = None, reach: Person | None = None, deps: ServiceDeps,
) -> None:
    """Delete a trigger and cascade-delete its subscriptions; needs owner on the trigger (Decision 3).

    The level is asked after the reach check; a refusal deletes nothing.

    ``reach`` is the calling session's: a trigger with a subscription that starts or feeds a session outside it answers :class:`TriggerNotFound`.

    The trigger's claim lease goes with its row (:func:`_delete_claim_lease`, ticket 01a16ede): a deleted trigger's
    lease would be read, and found gone, on every poll until it is dropped.
    """
    storage = deps.storage_provider.get_storage(Trigger)
    subs_storage = deps.storage_provider.get_storage(Subscription)
    await _require_trigger(trigger_id, person, AccessLevel.OWNER, deps)
    await _refuse_trigger_out_of_reach(trigger_id, reach, deps)
    # Cascade-delete all subscriptions bound to this trigger. All of them are read BEFORE any is deleted: deleting
    # while paging by offset shifts the remaining rows down, so every page after the first skipped as many rows as
    # the page before it had deleted (a trigger with more than 200 subscriptions left orphans behind).
    for sub in await list_subscriptions(trigger_id=trigger_id, deps=deps):
        try:
            await subs_storage.delete(sub.id)
        except NotFoundError:
            # Already gone (a concurrent delete) is fine; keep cascading. Any other failure propagates BEFORE the
            # trigger row is deleted: swallowing it left the subscription behind as an orphan, silently, and the
            # delete can be retried while the trigger is still there.
            pass
    await storage.delete(trigger_id)
    # The lease goes with the row: a deleted trigger's lease is read, and found gone, on every poll until it is dropped (ticket 01a16ede).
    await _delete_claim_lease(trigger_id, deps=deps)


async def fire_now(
    *, trigger_id: str, person: Person | None, reach: Person | None = None, deps: ServiceDeps,
):
    """Synchronously fire a trigger and return its :class:`FireResult`.

    The one manual fire path: ``POST /v1/triggers/{id}/fire_now`` and the ``trigger__fire_now`` tool both call it. Delegates to
    :func:`primer.trigger.dispatch.fire_trigger`. Needs full on the trigger (Decision 3): no level raises :class:`TriggerNotFound`, as does a
    subscription of it that starts or feeds a session outside ``reach`` (the calling session's); too low a level raises
    :class:`AccessDeniedError`. Either refusal fires nothing. Otherwise returns whatever ``fire_trigger`` reports (including the skipped=True
    envelope when the trigger is disabled). The fire runs as the trigger's and its subscriptions' owners, as every fire does.

    ``person`` is required (``None`` only for an internal call), so no door can fire without naming who fires.
    """
    await _require_trigger(trigger_id, person, AccessLevel.FULL, deps)
    await _refuse_trigger_out_of_reach(trigger_id, reach, deps)

    from primer.trigger.dispatch import fire_trigger
    from primer.trigger.subscribers import DispatchDeps

    dispatch_deps = DispatchDeps(
        storage_provider=deps.storage_provider,
        claim_engine=deps.claim_engine,
        scheduler=deps.scheduler,
        workspace_registry=deps.workspace_registry,
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
    parallelism: Parallelism = "skip",
    description: str | None = None,
    enabled: bool = True,
    event_matcher=None,
    reply_target=None,
    owner: PrincipalRef | None = None,
    person: Person | None = None,
    reach: Person | None = None,
    deps: ServiceDeps,
) -> Subscription:
    """Create a subscription bound to ``trigger_id``; needs full on the trigger (Decision 3).

    ``owner`` is the caller; a fresh-session run the subscription fires is
    attributed to it (see :mod:`primer.trigger.owner`). The owner must hold full on the target
    workspace (access-control P2 T6): a target they hold no full on raises
    :class:`SubscriptionTargetNotFound` and nothing is stored, and every fire checks the owner
    again (:mod:`primer.trigger.reach`).

    ``reach`` is the calling person (:func:`primer.trigger.reach.caller_scope`); it is not stored.

    Subscriptions with ``kind='parked_session'`` are reserved for the
    ``subscribe_to_trigger`` yielding tool — the REST + toolset create
    paths reject them.
    """
    if event_matcher is not None:
        validate_text_pattern(event_matcher.text_pattern)
    trigger = await _require_trigger(trigger_id, person, AccessLevel.FULL, deps)
    if config.kind == "parked_session":
        raise ParkedSessionOnlyFromYield(
            "subscriptions of kind 'parked_session' are only created via "
            "the subscribe_to_trigger yielding tool"
        )
    if person is not None:
        # A channel trigger's events ARE channel content: binding onto it needs READ on its channel (access-control P3 SEC, finding 6).
        await _require_channel_read(deps.storage_provider, trigger.config, person, as_target=True)
    # The owner of the subscription must hold full on its target workspace (access-control P2 T6): a target they hold no full on is
    # answered exactly as one that does not exist, and nothing is stored.
    refusal = await refuse_target_out_of_access(deps.storage_provider, owner, config)
    if refusal is not None:
        raise SubscriptionTargetNotFound(refusal)
    await _validate_explicit_reply_target(reply_target, deps)
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
        owner=owner,
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
    parallelism: Parallelism | None = None,
    enabled: bool | None = None,
    description: Any = _UNSET,
    event_matcher: Any = _UNSET,
    reply_target: Any = _UNSET,
    owner: PrincipalRef | None = None,
    person: Person | None = None,
    reach: Person | None = None,
    deps: ServiceDeps,
) -> Subscription:
    """Partial update; needs full on the trigger (Decision 3).

    The caller becomes the owner, as in :func:`update_trigger`, and its
    reach becomes the row's: from a session outside prime, a subscription
    whose target is out of the caller's reach is refused with
    :class:`SubscriptionNotFound`, exactly as an unknown subscription id
    (review of #874, C1), and left as it was. The new owner must hold full
    on the target workspace (access-control P2 T6): an update whose owner
    does not is refused with :class:`SubscriptionTargetNotFound`, and left
    as it was.

    Nullable string fields (``payload_template``, ``description``) use
    the ``_UNSET`` sentinel so callers can leave them untouched while
    still being able to explicitly clear them by passing ``None``.
    """
    if event_matcher is not _UNSET and event_matcher is not None:
        validate_text_pattern(event_matcher.text_pattern)
    # The field is assigned below, which pydantic does not validate: an unchecked value used to be stored as given (ticket 01a16ddb).
    if parallelism is not None and parallelism not in PARALLELISM_VALUES:
        raise ValueError(f"parallelism must be one of {', '.join(PARALLELISM_VALUES)}, not {parallelism!r}")
    if reply_target is not _UNSET:
        refuse_retired_reply_target(reply_target)       # the same reason: a retired dm_sender is refused here too (ticket 01a16e37-b98e)
        await _validate_explicit_reply_target(reply_target, deps)    # round 2 of #932, item 3: an explicit target names a real channel
    subs_storage = deps.storage_provider.get_storage(Subscription)
    sub = await _subscription_in_reach(trigger_id, subscription_id, reach, deps, person=person, need=AccessLevel.FULL)
    sub.owner = owner
    # The new owner must hold full on the target (access-control P2 T6): an update whose owner does not is
    # refused with the save's own answer, before anything is written.
    refusal = await refuse_target_out_of_access(deps.storage_provider, sub.owner, sub.config)
    if refusal is not None:
        raise SubscriptionTargetNotFound(refusal)
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


async def _subscription_in_reach(
    trigger_id: str, subscription_id: str, reach: Person | None, deps: ServiceDeps,
    *, person: Person | None = None, need: AccessLevel = AccessLevel.READ,
) -> Subscription:
    """The subscription ``subscription_id`` of ``trigger_id``, else :class:`SubscriptionNotFound`.

    A subscription inherits its trigger's access: ``person`` needs ``need`` on the trigger (no level raises
    :class:`TriggerNotFound`, too low a level :class:`AccessDeniedError`), asked before the subscription is looked up.

    ``reach`` is the calling session's (:func:`primer.trigger.reach.caller_scope`): a subscription whose target is outside it raises the
    same :class:`SubscriptionNotFound` as an unknown id or another trigger's subscription (ticket 01a16c22; review of #874, C1), so the single
    subscription tools never confirm an id out of reach. ``None`` (an admin or the system principal) reaches every subscription.
    """
    await _require_trigger(trigger_id, person, need, deps)
    sub = await deps.storage_provider.get_storage(Subscription).get(subscription_id)
    if sub is None or sub.trigger_id != trigger_id or not await subscription_in_reach(deps.storage_provider, reach, sub):
        raise SubscriptionNotFound(subscription_id)
    return sub


async def delete_subscription(
    *, trigger_id: str, subscription_id: str, person: Person | None = None, reach: Person | None = None, deps: ServiceDeps,
) -> None:
    """Delete one subscription of ``trigger_id``; needs full on the trigger (Decision 3).

    ``reach`` is the calling session's: a subscription whose target is outside it raises :class:`SubscriptionNotFound`, exactly as an unknown id,
    and nothing is deleted (ticket 01a16c22).
    """
    await _subscription_in_reach(trigger_id, subscription_id, reach, deps, person=person, need=AccessLevel.FULL)
    await deps.storage_provider.get_storage(Subscription).delete(subscription_id)


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


async def get_trigger(*, trigger_id: str, person: Person | None = None, deps: ServiceDeps) -> Trigger:
    """One trigger; needs read on it (no level answers :class:`TriggerNotFound`)."""
    return await _require_trigger(trigger_id, person, AccessLevel.READ, deps)


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
    *, trigger_id: str, owner: PrincipalRef | None = None, person: Person | None = None, deps: ServiceDeps,
) -> Trigger:
    """Rotate the capability token of a webhook trigger; needs owner on the trigger (Decision 3).

    The caller becomes the run-as owner: a rotate hands out the new token, so it can never leave the trigger ranked as
    someone higher. The row is written with :func:`primer.access.service.update_keeping_access`.

    Returns the updated trigger with the new token.
    Raises :class:`TriggerNotFound` if missing, or ``ValueError`` if the
    trigger is not of kind='webhook'.
    """
    storage = deps.storage_provider.get_storage(Trigger)
    trigger = await _require_trigger(trigger_id, person, AccessLevel.OWNER, deps)
    if trigger.config.kind != "webhook":
        raise ValueError(
            f"rotate_webhook_token requires kind='webhook', got {trigger.config.kind!r}"
        )
    new_token = _mint_webhook_token()
    trigger.config = trigger.config.model_copy(update={"token": new_token})
    trigger.owner = owner
    return await update_keeping_access(storage, trigger, trigger, what=f"Trigger {trigger_id!r}")


async def get_subscription(
    *, trigger_id: str, subscription_id: str, person: Person | None = None, reach: Person | None = None, deps: ServiceDeps,
) -> Subscription:
    """One subscription of ``trigger_id``; needs read on the trigger; ``reach`` as in :func:`delete_subscription` (out of reach is an unknown id)."""
    return await _subscription_in_reach(trigger_id, subscription_id, reach, deps, person=person)


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
    person: Person | None = None,
    deps: ServiceDeps,
) -> list[Trigger]:
    """Return ALL triggers ``person`` can read (optionally filtered), paging through every row.

    The access filter runs in the storage query (``visible_predicate``, GIN-indexed), never over fetched rows; an admin and an
    internal call (``person=None``) are unfiltered."""
    storage = deps.storage_provider.get_storage(Trigger)
    clauses = []
    if kind is not None:
        clauses.append(Q(Trigger).where_op("config.kind", Op.EQ, kind).build())
    if enabled is not None:
        clauses.append(Q(Trigger).where_op("enabled", Op.EQ, enabled).build())
    visible = visible_predicate(person) if person is not None else None
    if visible is not None:
        clauses.append(visible)
    if not clauses:
        return await _drain_list(storage)
    predicate = clauses[0]
    for clause in clauses[1:]:
        predicate = Predicate(left=predicate, op=Op.AND, right=clause)
    return await _drain_list(storage, predicate=predicate)


async def list_subscriptions(
    *, trigger_id: str, person: Person | None = None, reach: Person | None = None, deps: ServiceDeps,
) -> list[Subscription]:
    """Return ALL subscriptions for a trigger, paging through every row
    (previously capped at the first 200).

    ``reach`` is the calling session's: a subscription whose target is
    outside it is left out (ticket 01a16c22). The filter runs over the
    drained rows, never per page: the storage query cannot tell a session
    target's workspace (that is the target session's row), and a page is
    read in full whatever share of it is in reach, so no page short of
    in-reach rows ends the read and no row is skipped. ``None`` (prime, a
    person, an API client, and the trigger-level checks and the delete
    cascade, which must see every subscription) returns them all.
    """
    if person is not None:
        await _require_trigger(trigger_id, person, AccessLevel.READ, deps)
    subs_storage = deps.storage_provider.get_storage(Subscription)
    q = Q(Subscription).where_op("trigger_id", Op.EQ, trigger_id)
    rows = await _drain_list(subs_storage, predicate=q.build())
    if reach is None:
        return rows
    return [sub for sub in rows if await subscription_in_reach(deps.storage_provider, reach, sub)]


__all__ = [
    "ParkedSessionOnlyFromYield",
    "ServiceDeps",
    "SubscriptionNotFound",
    "SubscriptionTargetNotFound",
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
