"""Which sessions a trigger subscription may start or feed (review of #870, F1; tickets 01a16ba1-da9b and 01a16ba1-e2d1).

A subscription is a deferred tool call: when its trigger fires, it starts a session in a workspace (``agent_fresh_session``,
``graph_fresh_session``) or puts text into an existing session (``session_append``, ``parked_session``). The owner of a subscription must hold
full on the workspace the subscription starts or feeds a session in, asked twice (access-control P2 T6: the reach of the session that saved it
is no longer the rule):

* when it is saved (:func:`refuse_target_out_of_access`, from :func:`primer.trigger.service.create_subscription` and ``update_subscription``): a
  target the owner holds no full on is refused in the words of a target that does not exist, and nothing is stored, so a workspace or session id
  seen in a file is never confirmed;
* when it fires (:func:`refuse_fire_out_of_reach`, from every dispatcher in :mod:`primer.trigger.subscribers`): the owner is recorded on the row
  (``Subscription.owner``), and a fire whose owner holds no full on the target is refused (``target_out_of_reach``, a failed delivery the
  subscription records) and logged, and starts and feeds no session. That covers a grant revoked after the save, and a parked or appended-to
  session that moved since the row was saved.

A save with no identity (``owner`` ``None``) checks nothing at save time, but such a row never fires: migration 26 gave every subscription
saved before owners were recorded its trigger's owner, so a fire of a subscription with no owner is refused (fail closed, access-control P3
Task 4).

A trigger is not in a workspace, but its subscriptions are, so the tools that act on a whole trigger ask too (review of #874, B1): for a non-admin
caller (:func:`caller_scope`), ``trigger::update``, ``trigger::fire_now`` and ``trigger::delete`` answer exactly as an unknown trigger when any
subscription of the trigger targets a workspace the caller holds no full on (:func:`subscription_reaches_outside`). Without that, re-enabling or
firing a person's trigger would start a session in, or wake a session parked in, a workspace the caller cannot write.

The tools that read, update or delete ONE subscription, or list a trigger's subscriptions, ask of each subscription (ticket 01a16c22,
:func:`subscription_in_reach`): one whose target the caller holds no full on is left out of a list and answered exactly as an unknown
subscription id, so a target id seen nowhere else is never served, and a delete removes nothing.
"""

from __future__ import annotations

import logging
from typing import Any

from primer.access.person import Person, person_for_tool, person_from_ref
from primer.access.policy import level_of
from primer.access.workspaces import workspace_level
from primer.model.access import LEVEL_RANK, AccessLevel
from primer.model.principal import PrincipalRef
from primer.model.trigger import Trigger
from primer.model.trigger import (
    AgentFreshSubConfig,
    GraphFreshSubConfig,
    ParkedSessionSubConfig,
    SessionAppendSubConfig,
    Subscription,
)
from primer.model.workspace_session import WorkspaceSession

logger = logging.getLogger(__name__)

#: The ``error_code`` of a fire refused because its target is outside the reach recorded on its subscription.
TARGET_OUT_OF_REACH = "target_out_of_reach"


def _full(level: AccessLevel | None) -> bool:
    return level is not None and LEVEL_RANK[level] >= LEVEL_RANK[AccessLevel.FULL]


def caller_scope(ctx: Any) -> Person | None:
    """The person a trigger tool call acts as (:func:`primer.access.person.person_for_tool`), or ``None`` for an admin (nothing is filtered)."""
    person = person_for_tool(ctx)
    return None if person.is_admin else person


async def _target_refusal(storage_provider: Any, person: Person, config: Any) -> str | None:
    """Why ``person`` may not point a subscription of ``config`` at its target, else ``None``: they must hold full on the target workspace.

    A workspace the person holds no full on is refused in the words of a workspace that does not exist, and a target session the person
    holds no full on, or that does not exist at all, in the words a missing session answers in.
    """
    from primer.session.peer_messages import REFUSE_NOT_FOUND

    if isinstance(config, AgentFreshSubConfig | GraphFreshSubConfig):
        if _full(await workspace_level(storage_provider, person, config.workspace_id)):
            return None
        return f"Workspace {config.workspace_id!r} does not exist"
    if isinstance(config, SessionAppendSubConfig | ParkedSessionSubConfig):
        row = await storage_provider.get_storage(WorkspaceSession).get(config.session_id)
        if row is not None and _full(await workspace_level(storage_provider, person, row.workspace_id)):
            return None
        return REFUSE_NOT_FOUND.format(session_id=config.session_id)
    # A kind added later without a rule here is refused until it gets one (fail closed).
    return f"a {getattr(config, 'kind', '?')} subscription cannot be saved until its kind names a workspace its owner is checked on"


async def refuse_target_out_of_access(storage_provider: Any, owner: PrincipalRef | None, config: Any) -> str | None:
    """The refusal of a subscription ``config`` saved for an ``owner`` that does not hold full on its target, else ``None`` (access-control P2 T6).

    ``owner`` ``None`` (a save with no identity) refuses nothing.
    """
    if owner is None:
        return None
    return await _target_refusal(storage_provider, person_from_ref(owner), config)


async def subscription_in_reach(storage_provider: Any, reach: Person | None, sub: Subscription) -> bool:
    """Whether the caller ``reach`` (:func:`caller_scope`) may see or act on the one subscription ``sub`` (ticket 01a16c22).

    The question of every tool that names a single subscription or lists them: one the caller holds no full on the target of is left out of a
    list and answered exactly as an unknown subscription id. ``reach`` ``None`` (an admin) sees every subscription.
    """
    return reach is None or await _target_refusal(storage_provider, reach, sub.config) is None


#: What a caller reads about the tools that name one subscription (ticket 01a16c22).
SUBSCRIPTION_REACH_NOTE = (
    " A subscription whose target workspace you do not hold full access to answers ``type=subscription_not_found``, as an unknown id does."
)

#: What a caller reads about the tools that list a trigger's subscriptions (ticket 01a16c22).
SUBSCRIPTION_LIST_REACH_NOTE = (
    " Only the subscriptions whose target workspace you hold full access to are listed; the others are left out, as if they did not exist."
)


async def subscription_reaches_outside(storage_provider: Any, reach: Person | None, sub: Subscription) -> bool:
    """Whether ``sub`` starts or feeds a session in a workspace the caller ``reach`` holds no full on.

    The question the trigger-level tools ask of every subscription of a trigger (review of #874, B1): updating, firing or deleting the trigger acts on
    all of them. A target session that no longer exists is not outside (there is nothing to feed), and a kind with no rule here is (fail closed).
    """
    if reach is None:
        return False
    config = sub.config
    if isinstance(config, SessionAppendSubConfig | ParkedSessionSubConfig):
        if await storage_provider.get_storage(WorkspaceSession).get(config.session_id) is None:
            return False
    return await _target_refusal(storage_provider, reach, config) is not None


async def _refuse_lost_trigger_access(sub: Subscription, storage_provider: Any) -> str | None:
    """Why a fire of ``sub`` must not run because someone lost access to its TRIGGER, else ``None`` (access-control P3 SEC, findings 3 and 7).

    * the trigger's run-as owner (``trigger.owner``, whoever last saved it) must still hold FULL on it: a revoked editor's save must not keep firing as them;
    * the subscription's owner must still hold READ on the trigger: a revoked grantee's subscriptions stop firing.

    An admin or system owner, an unattributed one and a trigger saved with no owner are not checked (nothing to lose); a trigger that is gone is left to the
    caller.
    """
    trigger = await storage_provider.get_storage(Trigger).get(sub.trigger_id)
    if trigger is None:
        return None
    checks = (
        (trigger.owner, AccessLevel.FULL, "the trigger's run-as owner no longer has access; an owner must save it"),
        (
            sub.owner,
            AccessLevel.READ,
            "the owner of this subscription no longer has access to its trigger; the fire starts and feeds no session (re-save it from a user that does)",
        ),
    )
    for ref, need, reason in checks:
        if ref is None or ref.type == "system" or (ref.type, ref.id) == (PrincipalRef.unattributed().type, PrincipalRef.unattributed().id):
            continue
        person = person_from_ref(ref)
        if person.is_admin:
            continue
        held = level_of(person, trigger.access)
        if held is None or LEVEL_RANK[held] < LEVEL_RANK[need]:
            logger.warning("trigger %s subscription %s refused: %s", sub.trigger_id, sub.id, reason)
            return reason
    return None


async def refuse_fire_out_of_reach(sub: Subscription, storage_provider: Any) -> str | None:
    """Why a fire of ``sub`` may not start or feed its target, or ``None`` when it may.

    Also refused when its trigger's run-as owner no longer holds FULL on the trigger, or the subscription's owner no longer holds READ on it
    (:func:`_refuse_lost_trigger_access`).

    The owner recorded on the row (``sub.owner``) must hold full on the target's workspace: the config's ``workspace_id`` for a fresh session,
    the target session's workspace for ``session_append`` and ``parked_session``. A row with no owner is refused (fail closed: migration 26 gave
    every legacy row its trigger's owner, so nobody could be checked), and a target session that no longer exists is not refused here: there is
    nothing to start or feed, and the dispatcher reports the missing session in its own words. A kind with no rule is refused (fail closed). The reason is stored on the subscription
    (``last_fire_error``), read by whoever may read the row.
    """
    lost = await _refuse_lost_trigger_access(sub, storage_provider)
    if lost is not None:
        return lost
    owner = getattr(sub, "owner", None)
    if owner is None:
        logger.warning("trigger %s subscription %s refused: it has no owner; the fire starts and feeds no session", sub.trigger_id, sub.id)
        return "this subscription has no owner, so nobody can be checked on its target; the fire starts and feeds no session (re-save it)"
    if person_from_ref(owner).is_admin:
        return None  # an admin or system owner reaches every workspace, one whose row is gone included (as require_session)
    config = sub.config
    if isinstance(config, AgentFreshSubConfig | GraphFreshSubConfig):
        target: str | None = config.workspace_id
    elif isinstance(config, SessionAppendSubConfig | ParkedSessionSubConfig):
        row = await storage_provider.get_storage(WorkspaceSession).get(config.session_id)
        if row is None:
            return None
        target = row.workspace_id
    else:
        target = None  # a kind with no rule here: refused (fail closed)
    level = await workspace_level(storage_provider, person_from_ref(owner), target) if target is not None else None
    if target is not None and _full(level):
        return None
    logger.warning(
        "trigger %s subscription %s refused: its owner %s holds no full on its target workspace %s; the fire starts and feeds no session",
        sub.trigger_id, sub.id, owner.display, target,
    )
    return (
        f"the owner of this subscription holds no full on its target workspace {target!r}; "
        "the fire starts and feeds no session (re-save it from a user that does)"
    )


__all__ = [
    "SUBSCRIPTION_LIST_REACH_NOTE",
    "SUBSCRIPTION_REACH_NOTE",
    "TARGET_OUT_OF_REACH",
    "caller_scope",
    "refuse_fire_out_of_reach",
    "refuse_target_out_of_access",
    "subscription_in_reach",
    "subscription_reaches_outside",
]
