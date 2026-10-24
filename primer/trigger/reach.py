"""Which sessions a trigger subscription may start or feed (review of #870, F1; tickets 01a16ba1-da9b and 01a16ba1-e2d1).

A subscription is a deferred tool call: when its trigger fires, it starts a session in a workspace (``agent_fresh_session``,
``graph_fresh_session``) or puts text into an existing session (``session_append``, ``parked_session``). Saved from a workspace session, it must
not reach further than that session could itself, or a session outside the default workspace (prime) could start a run in prime, which reaches
every workspace, by subscribing to a trigger. So the reach of the session that saves it (:func:`primer.session.peer_messages.caller_reach`) is
checked twice:

* when it is saved (:func:`refuse_target_out_of_reach`, from :func:`primer.trigger.service.create_subscription` and ``update_subscription``): a
  target out of reach is refused in the words of a target that does not exist, and nothing is stored, so a workspace or session id seen in a file
  is never confirmed;
* when it fires (:func:`refuse_fire_out_of_reach`, from every dispatcher in :mod:`primer.trigger.subscribers`): the reach is recorded on the row
  (``Subscription.reach``), and a fire whose target is outside it is refused (``target_out_of_reach``, a failed delivery the subscription records)
  and logged, and starts and feeds no session. That covers a row written past the save check, and a parked or appended-to session that is not
  where it was when the row was saved.

A subscription saved from prime, by a person (REST) or by an API client (MCP) records no reach (``None``, every workspace), and so does every row
saved before reach was recorded: those fire as they always did.

A trigger is not in a workspace, but its subscriptions are, so the tools that act on a whole trigger ask too (review of #874, B1): from a session
outside prime, ``trigger::update``, ``trigger::fire_now`` and ``trigger::delete`` answer exactly as an unknown trigger when any subscription of the
trigger reaches outside the caller's reach (:func:`subscription_reaches_outside`). Without that, re-enabling or firing a person's trigger would start
a session in prime, or wake a session parked in another workspace.

The tools that read, update or delete ONE subscription, or list a trigger's subscriptions, ask of each subscription (ticket 01a16c22,
:func:`subscription_in_reach`): from a session outside prime one whose target is out of reach is left out of a list and answered exactly as an
unknown subscription id, so a target id seen nowhere else is never served, and a delete removes nothing.
"""

from __future__ import annotations

import logging
from typing import Any

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


async def refuse_target_out_of_reach(storage_provider: Any, reach: str | None, config: Any) -> str | None:
    """The refusal of a subscription ``config`` saved by a caller whose reach is ``reach``, else ``None``.

    ``reach`` ``None`` (prime, a person, an API client) refuses nothing. From a session outside prime the target must be its own workspace: a
    fresh-session config naming another workspace is answered as a workspace that does not exist, and a config naming a session that is not in
    the caller's workspace, or does not exist at all, as a session that does not exist in it (the words ``message_session`` answers in). An
    unknown target is refused too, so an out-of-reach id and an unknown one answer alike.
    """
    if reach is None:
        return None
    from primer.session.peer_messages import REFUSE_NOT_FOUND, in_reach

    if isinstance(config, AgentFreshSubConfig | GraphFreshSubConfig):
        return None if in_reach(reach, config.workspace_id) else f"Workspace {config.workspace_id!r} does not exist"
    if isinstance(config, SessionAppendSubConfig | ParkedSessionSubConfig):
        row = await storage_provider.get_storage(WorkspaceSession).get(config.session_id)
        if row is not None and in_reach(reach, row.workspace_id):
            return None
        return REFUSE_NOT_FOUND.format(session_id=config.session_id)
    # A kind added later without a rule here is refused from a session outside prime until it gets one (fail closed).
    return f"a {getattr(config, 'kind', '?')} subscription cannot be saved from a session outside the default workspace (prime)"


async def subscription_in_reach(storage_provider: Any, reach: str | None, sub: Subscription) -> bool:
    """Whether a caller whose reach is ``reach`` may see or act on the one subscription ``sub`` (ticket 01a16c22).

    The question of every tool that names a single subscription or lists them (``list_subscriptions``, ``get_subscription``,
    ``update_subscription``, ``delete_subscription``, and the channel-binding twins ``list_channel_bindings`` and ``delete_channel_binding``):
    one that is not in reach is left out of a list and answered exactly as an unknown subscription id. The target is classified as the save
    check classifies it (:func:`refuse_target_out_of_reach`), so what a session may save, read, update and delete agree: ``reach`` ``None``
    (prime, a person, an API client) reaches every subscription; otherwise a fresh-session subscription is in reach when its ``workspace_id`` is
    the caller's workspace, a ``session_append`` or ``parked_session`` one when its target session is in the caller's workspace. A target session
    that no longer exists is in no workspace, so out of reach (from prime or as a person it is still read and deleted), and a kind with no rule
    there is out of reach (fail closed).
    """
    return await refuse_target_out_of_reach(storage_provider, reach, sub.config) is None


#: What a session outside prime reads about the tools that name one subscription (ticket 01a16c22).
SUBSCRIPTION_REACH_NOTE = (
    " From a session outside the default workspace (prime) a subscription whose target is not your own workspace or a session in it answers "
    "``type=subscription_not_found``, as an unknown id does."
)

#: What a session outside prime reads about the tools that list a trigger's subscriptions (ticket 01a16c22).
SUBSCRIPTION_LIST_REACH_NOTE = (
    " From a session outside the default workspace (prime) only the subscriptions whose target is your own workspace or a session in it are "
    "listed; the others are left out, as if they did not exist."
)


async def subscription_reaches_outside(storage_provider: Any, reach: str | None, sub: Subscription) -> bool:
    """Whether ``sub`` starts or feeds a session outside ``reach`` (a caller's reach, :func:`primer.session.peer_messages.caller_reach`).

    The question the trigger-level tools ask of every subscription of a trigger (review of #874, B1): updating, firing or deleting the trigger acts on
    all of them. ``reach`` ``None`` (prime, a person, an API client) reaches everything. A fresh-session subscription reaches outside when its
    ``workspace_id`` is not the caller's workspace; a ``session_append`` or ``parked_session`` one when its target session is in another workspace.
    A target session that no longer exists is not outside (there is nothing to feed), and a kind with no rule here is (fail closed).
    """
    if reach is None:
        return False
    from primer.session.peer_messages import in_reach

    config = sub.config
    if isinstance(config, AgentFreshSubConfig | GraphFreshSubConfig):
        return not in_reach(reach, config.workspace_id)
    if isinstance(config, SessionAppendSubConfig | ParkedSessionSubConfig):
        row = await storage_provider.get_storage(WorkspaceSession).get(config.session_id)
        return row is not None and not in_reach(reach, row.workspace_id)
    return True


async def refuse_fire_out_of_reach(sub: Subscription, storage_provider: Any) -> str | None:
    """Why a fire of ``sub`` may not start or feed its target, or ``None`` when it may.

    ``sub.reach`` ``None`` (every workspace) never refuses. Otherwise the target's workspace must be the recorded one: the config's
    ``workspace_id`` for a fresh session, the target session's workspace for ``session_append`` and ``parked_session``. A target session that no
    longer exists is not refused here: there is nothing to start or feed, and the dispatcher reports the missing session in its own words. The
    reason is stored on the subscription (``last_fire_error``), read by whoever may read the row, so it names the recorded reach (the saver's own
    workspace) and never the target's.
    """
    reach = getattr(sub, "reach", None)
    if reach is None:
        return None
    from primer.session.peer_messages import in_reach

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
    if target is not None and in_reach(reach, target):
        return None
    logger.warning(
        "trigger %s subscription %s refused: its target is outside the reach recorded on it (workspace %s); the fire starts and feeds no session",
        sub.trigger_id, sub.id, reach,
    )
    return (
        f"this subscription was saved from a session that reaches only workspace {reach!r}, and its target is outside that reach; "
        "the fire starts and feeds no session (re-save it from a session that reaches the target, from prime or as a person)"
    )


__all__ = [
    "SUBSCRIPTION_LIST_REACH_NOTE",
    "SUBSCRIPTION_REACH_NOTE",
    "TARGET_OUT_OF_REACH",
    "refuse_fire_out_of_reach",
    "refuse_target_out_of_reach",
    "subscription_in_reach",
    "subscription_reaches_outside",
]
