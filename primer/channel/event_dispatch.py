"""ChannelEventRouter: correlation-first inbound precedence over channel triggers.

A normalized :class:`~primer.model.channel_event.ChannelEvent` arrives from a
provider's inbound gateway and is routed here. The precedence is:

  1. Correlation-first. When the event carries a ``thread_anchor`` and a durable
     :class:`~primer.model.channel_correlation.ChannelCorrelation` exists for
     ``(channel_id, thread_anchor)``, the event is a reply to a known artefact:

       * ``kind="session"`` -> publish ``ask_user:{sid}:{tcid}`` so the parked
         session gate resumes (mirrors the legacy
         :class:`~primer.channel.inbound_router.ChannelInboundRouter` gate path).
       * ``kind="session"`` with NO open gate -> the reply is the next user
         message on that thread's mapped session, delivered through
         :func:`~primer.session.steer_delivery.deliver_steer`.

     Either way the router returns: a correlated reply NEVER fans out to
     channel triggers.

  2. Otherwise it is a fresh inbound event. Resolve every ``kind="channel"``
     :class:`~primer.model.trigger.Trigger` whose ``provider_id`` matches and
     whose ``channel_id`` is either unset (provider-wide) or equal to this
     channel, and fire each via the injected ``fire_trigger`` with the event in
     ``extra_context["event"]``. Per-trigger failures are isolated.

Returns a :class:`ChannelRouteOutcome` naming what it did.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from primer.channel.correlation import CorrelationStore
from primer.channel.reply_binding import SESSION_REPLY_BINDING_KEY
from primer.int.storage_provider import StorageProvider
from primer.model.channel import Channel
from primer.model.channel_event import ChannelEvent
from primer.model.envelope import RELAY_EVERY_TURN_KEY
from primer.model.event_matcher import channel_event_text_budget
from primer.model.storage import Op, OffsetPage
from primer.model.trigger import Trigger
from primer.model.workspace_session import SessionStatus, WorkspaceSession
from primer.session.steer_delivery import DELIVERED_MISSING, deliver_steer
from primer.storage.q import Q
from primer.trigger.dispatch import enabled_subscriptions, owner_keys
from primer.trigger.dispatch import fire_trigger as _default_fire_trigger
from primer.trigger.subscribers import DispatchDeps


logger = logging.getLogger(__name__)


def mapping_anchor(event: ChannelEvent) -> str | None:
    """The correlation key for this event's conversation, or None.

    A threaded message keys on its platform thread id. A DM has no thread,
    but S6 section 5 makes the DM itself the thread for mapping purposes, so
    it keys on the sender inside that channel. A bare room post that opened
    no thread has no conversation to continue and always fires fresh.
    """
    if event.thread_anchor:
        return event.thread_anchor
    if event.surface == "dm" and event.sender is not None:
        return f"dm:{event.sender.external_id}"
    return None


@dataclass
class ChannelRouteOutcome:
    """What routing one inbound event actually did.

    ``kind`` is ``"gate"`` (resumed a parked gate), ``"steer"`` (appended to
    the thread's mapped session), ``"fired"`` (no mapping: fired the channel
    triggers) or ``"ignored"`` (nothing matched).
    """

    kind: str
    session_id: str | None = None
    fire_ids: list[str] = field(default_factory=list)


#: Inbound events already routed by this process (W33), oldest first: ``(provider_id, room_external_id, type, event_id)``
#: -> when first seen (monotonic). A platform redelivers an event it thinks was missed (a Slack ack later than 3 s, an
#: unconfirmed Telegram update); routing it again would start a second session or steer twice.
_SEEN_EVENTS: OrderedDict[tuple, float] = OrderedDict()
_SEEN_EVENTS_MAX = 10_000
_SEEN_EVENTS_TTL_SECONDS = 600.0


def _first_delivery(key: tuple) -> bool:
    """Record ``key`` and answer whether this is its first delivery inside the TTL. The set stays bounded."""
    now = time.monotonic()
    while _SEEN_EVENTS:
        oldest, seen_at = next(iter(_SEEN_EVENTS.items()))
        if now - seen_at < _SEEN_EVENTS_TTL_SECONDS and len(_SEEN_EVENTS) < _SEEN_EVENTS_MAX:
            break
        del _SEEN_EVENTS[oldest]
    if key in _SEEN_EVENTS:
        return False
    _SEEN_EVENTS[key] = now
    return True


#: Conversations being routed in this process (W6): ``(channel_id, mapping anchor)`` -> ``[lock, users]``. One event of a
#: conversation routes at a time, in arrival order, so a second quick message finds the session the first one started.
#: An entry lives only while an event of its conversation is routing, so the map is bounded by the events in flight.
_CONVERSATION_LOCKS: dict[tuple[str, str], list] = {}

#: Across processes the get-or-create of a conversation's session is a conditional insert (``CorrelationStore.claim_thread``).
#: A message that loses it waits up to this long for the winner's session, looking this often, then starts one itself.
_CLAIM_WAIT_SECONDS = 30.0
_CLAIM_POLL_SECONDS = 0.2


@contextlib.asynccontextmanager
async def _conversation_lock(key: tuple[str, str]):
    entry = _CONVERSATION_LOCKS.setdefault(key, [asyncio.Lock(), 0])
    entry[1] += 1
    try:
        async with entry[0]:
            yield
    finally:
        entry[1] -= 1
        if not entry[1]:
            del _CONVERSATION_LOCKS[key]


def _artefacts(result) -> list[tuple[str, str | None]]:
    """``(session id, subscription id)`` of every session a fire started or fed, in the order the subscriptions ran."""
    return [
        (envelope["artefact_id"], envelope.get("subscription_id"))
        for envelope in result.results
        if envelope.get("ok") and not envelope.get("skipped") and envelope.get("artefact_id")
    ]


#: The binding kinds that START a session. A room whose ``chats.enabled`` is false does not fire them (ticket 01a16e37-b98e, ruling 4);
#: ``session_append`` (feeds a running session) and ``parked_session`` (a session waiting on a channel event) fire as before.
SESSION_STARTING_KINDS: frozenset[str] = frozenset({"agent_fresh_session", "graph_fresh_session"})


#: Rooms whose session-starting bindings were dropped by :func:`_without_session_starts` and reported: the warning fires once per
#: room and process, not once per event (round 2 of #932, item 4).
_ROOM_DISABLED_REPORTED: set[str] = set()


def _without_session_starts(triggers: list, subscriptions: dict, *, event: ChannelEvent, channel_id: str | None) -> tuple[list, dict]:
    """Drop the bindings that would start a session in a room whose ``chats.enabled`` is false.

    The subscriptions are the ones ``fire_trigger`` reads through the text budget, so a dropped binding is never matched or dispatched.
    A trigger left with no binding by the drop is not fired at all (it would record a fire that did nothing); one that had none to begin
    with fires as before. Counted once per event in ``channel_events_skipped_total{reason="room_disabled"}``.
    """
    from primer.observability import metrics

    kept_triggers: list = []
    kept: dict = {}
    dropped = 0
    dropped_triggers: list[str] = []
    for trigger in triggers:
        subs = subscriptions.get(trigger.id, [])
        remaining = [sub for sub in subs if sub.config.kind not in SESSION_STARTING_KINDS]
        gone = len(subs) - len(remaining)
        if gone:
            dropped_triggers.append(trigger.id)
        dropped += gone
        kept[trigger.id] = remaining
        if remaining or not subs:
            kept_triggers.append(trigger)
    if dropped:
        metrics.channel_events_skipped_total.labels(reason="room_disabled").inc()
        # Once per room and process (round 2 of #932, item 4): the steady-state drop is expected, but the FIRST one must say the room
        # and its triggers, so an operator who sees no session start can find the switch that stopped it.
        room_key = channel_id or (event.room_external_id or "?")
        if room_key not in _ROOM_DISABLED_REPORTED:
            _ROOM_DISABLED_REPORTED.add(room_key)
            logger.warning(
                "channel event %s: room %s has chats.enabled false; %d binding(s) that start a session were not fired (triggers %s); "
                "the room will not start sessions until the channel is re-enabled",
                event.event_id, room_key, dropped, ", ".join(dropped_triggers),
            )
    return kept_triggers, kept


#: Subscriptions whose stored ``dm_sender`` has been reported, so the warning is logged once per subscription and process.
_DM_SENDER_REPORTED: set[str] = set()


def outbound_binding(
    reply_target,
    *,
    channel_id: str,
    thread_anchor: str | None,
    interactive: bool,
    maps_conversation: bool,
    subscription_id: str | None = None,
) -> dict | None:
    """The reply binding (``session.metadata`` form) a session started or fed by a binding gets, or None for none at all.

    ``reply_target`` is the binding's (ticket 01a16e37-b98e, ruling 2):

    * ``None`` (the default) is the behaviour from before reply targets were read: the session that maps the conversation (a
      thread, a DM) answers in it, and any other session gets no binding of its own (a room post: the workspace's reply binding).
    * ``source_thread``: the thread the message was posted in, else the room it was posted in (``thread_anchor`` None).
    * ``source_room``: the room it was posted in, with no thread (Slack and Discord open a thread per session there).
    * ``none``: a silent binding, so nothing is posted (``ReplyBinding.silent``) and the workspace's binding does not answer either.
    * an explicit ``{channel_id, anchor, quiet}``: that channel and thread (Telegram ignores the anchor).
    * a stored ``dm_sender`` (refused on save since) acts as ``source_thread``, with one warning per subscription.

    ``quiet`` is the channel trigger's ``interactive`` flag inverted (an explicit target's ``quiet`` adds to it): a quiet binding
    posts no turn answers, only questions, approval requests and updates.
    """
    quiet = not interactive
    if reply_target is None:
        if not maps_conversation:
            return None
        return {"channel_id": channel_id, "anchor": thread_anchor, "quiet": quiet}
    if reply_target == "dm_sender":
        if subscription_id not in _DM_SENDER_REPORTED:
            _DM_SENDER_REPORTED.add(subscription_id)
            logger.warning(
                "channel binding %s has reply_target 'dm_sender', which no channel can post to; its sessions answer in the "
                "source thread or room instead. Change the binding's reply_target.", subscription_id,
            )
        reply_target = "source_thread"
    if reply_target == "source_thread":
        return {"channel_id": channel_id, "anchor": thread_anchor, "quiet": quiet}
    if reply_target == "source_room":
        return {"channel_id": channel_id, "anchor": None, "quiet": quiet}
    if reply_target == "none":
        return {"channel_id": channel_id, "anchor": thread_anchor, "quiet": True, "silent": True}
    return {
        "channel_id": reply_target.channel_id,
        "anchor": reply_target.anchor,
        "quiet": quiet or bool(reply_target.quiet),
        # The target's anchor is a SHARED one (round 2, finding 1): every session of this binding posts into the same thread,
        # so an ask_user question opens the session's own thread and the anchor carries no thread-to-session row.
        "shared_anchor": True,
    }


class ChannelEventRouter:
    """Route a normalized ``ChannelEvent`` correlation-first, else fire rules."""

    def __init__(
        self,
        *,
        storage_provider: StorageProvider,
        correlation_store: CorrelationStore,
        fire_deps: DispatchDeps,
        event_bus=None,
        fire_trigger=_default_fire_trigger,
        artifact_registry=None,
    ) -> None:
        self._sp = storage_provider
        self._correlation = correlation_store
        self._fire_deps = fire_deps
        self._bus = event_bus
        self._fire_trigger = fire_trigger
        self._artifacts = artifact_registry

    async def route_event(
        self,
        *,
        event: ChannelEvent,
        channel: Channel | None,
        media_parts: list | None = None,
    ) -> ChannelRouteOutcome:
        """Route one normalized inbound event and report what it did.

        A redelivery of an event this process already routed (same provider, room, type and event id) is ignored.
        """
        key = (event.provider_id, event.room_external_id, getattr(event.type, "value", event.type), event.event_id)
        if event.event_id and not _first_delivery(key):
            logger.info("channel event %s in %s was delivered again; already routed", event.event_id, event.room_external_id)
            return ChannelRouteOutcome(kind="ignored")
        try:
            channel_id = event.channel_id or (channel.id if channel is not None else None)
            anchor = mapping_anchor(event)
            if anchor is None or channel_id is None:
                return await self._route(event=event, channel=channel, media_parts=media_parts)
            async with _conversation_lock((channel_id, anchor)):
                return await self._route(event=event, channel=channel, media_parts=media_parts)
        except BaseException:
            _SEEN_EVENTS.pop(key, None)  # routing failed: a redelivery may try again
            raise

    async def _route(
        self,
        *,
        event: ChannelEvent,
        channel: Channel | None,
        media_parts: list | None,
        claim: bool = True,
    ) -> ChannelRouteOutcome:
        channel_id = event.channel_id or (channel.id if channel is not None else None)

        # The SDK-free normalizers only set ``room_external_id`` (the platform
        # room id), not the internal ``channel_id``. Stamp the resolved
        # internal id onto the event so the thread mapper and the reply
        # binding it writes resolve back to this channel's adapter. Without
        # it the created session has no reply route.
        if channel is not None and not event.channel_id:
            event.channel_id = channel.id

        # ----- (1) Correlation-first -----------------------------------
        anchor = mapping_anchor(event)
        record = None
        if anchor and channel_id is not None:
            record = await self._correlation.lookup(channel_id, anchor)
            if record is not None and record.session_id is None:
                # A claim: another process is starting this conversation's session (W6). Wait for it, then route again.
                if claim:
                    await self._await_claim(channel_id, anchor)
                    return await self._route(event=event, channel=channel, media_parts=media_parts, claim=False)
                record = None  # the claimer never finished: start the conversation here
            if record is not None and record.kind == "session" and record.tool_call_id:
                # The parked turn this gate would resume may no longer
                # exist: the session can have ended (e.g. its yield timed
                # out) before the reply arrived, and a stale correlation
                # record still carries the dead tool_call_id. Publishing
                # onto that event_key goes nowhere - nothing is listening
                # for an ENDED session's resume key - so this used to
                # silently drop the reply instead of erroring loudly, and
                # if something DID still hold a lease it could re-arm the
                # claim for an ENDED row, which is what raised "cannot
                # invoke ENDED session" at turn time. Check first and, when
                # ended, fall back to the SAME reopen-and-steer path a
                # plain inbound message already uses below, so any inbound
                # message reopens an ended session uniformly.
                sessions = self._sp.get_storage(WorkspaceSession)
                target = await sessions.get(record.session_id)
                if target is not None and target.status == SessionStatus.ENDED:
                    await self._correlation.clear_gate(channel_id, anchor, record.tool_call_id)
                    delivery = await deliver_steer(
                        session_id=record.session_id,
                        text=await self._steer_text(
                            event=event,
                            media_parts=media_parts,
                            workspace_ids=[target.workspace_id],
                            fire_id=event.event_id,
                        ),
                        parallelism="queue",
                        # 01a08c08: an ended-session reopen from an inbound
                        # channel reply is still a person on the other end.
                        human_intent=True,
                        storage_provider=self._sp,
                        scheduler=self._fire_deps.scheduler,
                        claim_engine=self._fire_deps.claim_engine,
                        workspace_registry=self._fire_deps.workspace_registry,
                        event_bus=self._bus,
                        # Channel-thread senders are not primer users: the turn runs as the session's creator (spec §13 Q1).
                        by=target.initiated_by,
                    )
                    if delivery.outcome != DELIVERED_MISSING:
                        return ChannelRouteOutcome(
                            kind="steer", session_id=record.session_id,
                        )
                    logger.info(
                        "channel event: gate target %s/%s ended and could "
                        "not be reopened; treating as a new thread",
                        channel_id, anchor,
                    )
                else:
                    if self._bus is None:
                        logger.warning(
                            "channel event: session correlation for %s but "
                            "no event bus; dropping reply", channel_id,
                        )
                        return ChannelRouteOutcome(kind="ignored")
                    # The reply goes through the inbox like every adapter's direct reply: the key the pending gate WAITS on is resolved from the row (an
                    # ask_user inside a graph node waits on ``ask_user:{sid}:{node}:{tcid}``, which a key rebuilt here from the raw id would miss, so the
                    # answer was lost) and the C-033 stale fence applies, so a reply to a prompt that has since been replaced does not answer the newer one.
                    from primer.channel.inbox import ChannelInbox
                    from primer.model.envelope import ResponseEnvelope
                    from primer.session.gate_token import StaleGateError

                    try:
                        await ChannelInbox(event_bus=self._bus, storage_provider=self._sp).handle_response(ResponseEnvelope(
                            kind="ask_user", workspace_id=record.workspace_id, session_id=record.session_id,
                            tool_call_id=record.tool_call_id, response=event.text, decision=None, reason=None,
                            gate_id=getattr(record, "gate_id", None),
                        ))
                    except StaleGateError:
                        # The thread's prompt was replaced by a newer question: this text is not its answer. The stale gate is cleared and the text
                        # falls through to the steer below, like a reply in a thread with no open gate.
                        logger.info(
                            "channel event: reply for %s/%s names a prompt that has been replaced; steering instead", channel_id, anchor,
                        )
                        await self._correlation.clear_gate(channel_id, anchor, record.tool_call_id)
                    else:
                        # One reply answers one gate. Clearing it here is what
                        # lets the NEXT reply in the same thread steer the
                        # session instead of re-publishing onto a dead resume
                        # key.
                        await self._correlation.clear_gate(channel_id, anchor, record.tool_call_id)
                        return ChannelRouteOutcome(
                            kind="gate", session_id=record.session_id,
                        )
            if record is not None and record.kind == "session":
                # Thread-mapped session, no open gate: the reply is the next
                # user message on that session (S6 section 5). Queueing is
                # the parallelism a human conversation wants; a steer that
                # lands mid-turn is realized at the drain checkpoint.
                # Channel-thread senders are not primer users: the turn runs
                # as the session's creator (spec §13 Q1).
                target = await self._sp.get_storage(WorkspaceSession).get(record.session_id)
                delivery = await deliver_steer(
                    session_id=record.session_id,
                    text=await self._steer_text(
                        event=event,
                        media_parts=media_parts,
                        workspace_ids=[record.workspace_id],
                        fire_id=event.event_id,
                    ),
                    parallelism="queue",
                    # 01a08c08: a thread-mapped reply is a human conversation.
                    human_intent=True,
                    storage_provider=self._sp,
                    scheduler=self._fire_deps.scheduler,
                    claim_engine=self._fire_deps.claim_engine,
                    workspace_registry=self._fire_deps.workspace_registry,
                    event_bus=self._bus,
                    by=target.initiated_by if target is not None else None,
                )
                if delivery.outcome != DELIVERED_MISSING:
                    return ChannelRouteOutcome(
                        kind="steer", session_id=record.session_id,
                    )
                # The session was deleted but the thread lives on: fall
                # through so the fresh-thread path re-maps it.
                logger.info(
                    "channel event: mapping for %s/%s points at a deleted "
                    "session; treating as a new thread",
                    channel_id, anchor,
                )

        # ----- (2) Fresh event -> fire channel triggers ----------------
        triggers = await self._resolve_channel_triggers(
            event.provider_id, channel_id,
        )
        event_payload = event.model_dump(mode="json")
        extra_context = {"event": event_payload}
        # Every trigger this event fires shares ONE text_pattern match budget
        # (G6-5): fire_trigger reads it, so N triggers cannot multiply it. It
        # is split equally between the distinct owners of the text_pattern
        # subscriptions about to be matched (round 2, M1).
        # The subscriptions are read once here and handed to fire_trigger
        # through the budget (#859 round 3, R2-11); an API token counts as
        # its user when the shares are made (R2-1).
        subscriptions = {trigger.id: await enabled_subscriptions(self._sp, trigger.id) for trigger in triggers}
        if not await self._room_starts_sessions(channel_id, channel):
            triggers, subscriptions = _without_session_starts(triggers, subscriptions, event=event, channel_id=channel_id)
        claimed = False
        if claim and record is None and anchor is not None and channel_id is not None and triggers:
            # The get-or-create of the conversation's session is a conditional insert, not this method's lookup (W6):
            # losing it means another process is starting the session, so wait for it and steer into it.
            if not await self._correlation.claim_thread(channel_id=channel_id, anchor=anchor):
                await self._await_claim(channel_id, anchor)
                return await self._route(event=event, channel=channel, media_parts=media_parts, claim=False)
            claimed = True
        if media_parts:
            # The created session's FIRST instruction references the same landed files the steer path composes (W9): the
            # attachments land under media/<event id>/ in every workspace a binding about to fire starts a session in.
            event_payload["text"] = await self._steer_text(
                event=event,
                media_parts=media_parts,
                workspace_ids=[getattr(sub.config, "workspace_id", None) for subs in subscriptions.values() for sub in subs],
                fire_id=event.event_id,
            )
        try:
            return await self._fire_and_map(
                event=event, triggers=triggers, subscriptions=subscriptions, extra_context=extra_context,
                channel_id=channel_id, anchor=anchor,
            )
        finally:
            if claimed:
                await self._correlation.release_claim(channel_id=channel_id, anchor=anchor)

    async def _await_claim(self, channel_id: str, anchor: str) -> None:
        """Wait until another process's claim on a conversation names its session or is gone, at most the claim wait."""
        deadline = time.monotonic() + _CLAIM_WAIT_SECONDS
        while time.monotonic() < deadline:
            await asyncio.sleep(_CLAIM_POLL_SECONDS)
            record = await self._correlation.lookup(channel_id, anchor)
            if record is None or record.session_id is not None:
                return
        logger.warning(
            "channel event: conversation %s/%s was claimed but no session appeared in %.0fs; starting one here",
            channel_id, anchor, _CLAIM_WAIT_SECONDS,
        )

    async def _fire_and_map(
        self,
        *,
        event: ChannelEvent,
        triggers: list,
        subscriptions: dict,
        extra_context: dict,
        channel_id: str | None,
        anchor: str | None,
    ) -> ChannelRouteOutcome:
        """Fire the matched triggers and map the conversation to the first session they start or feed."""
        fire_ids: list[str] = []
        mapped_session_id: str | None = None
        keys = await owner_keys(self._sp, [sub for subs in subscriptions.values() for sub in subs])
        with channel_event_text_budget(owner_keys=keys, subscriptions=subscriptions):
            for trigger in triggers:
                try:
                    result = await self._fire_trigger(
                        trigger_id=trigger.id,
                        scheduled_for=None,
                        deps=self._fire_deps,
                        extra_context=extra_context,
                    )
                except Exception:  # noqa: BLE001 -- isolate per-trigger failures
                    logger.exception(
                        "channel event: fire_trigger raised for %s", trigger.id,
                    )
                    continue
                fire_id = getattr(result, "fire_id", None)
                if fire_id is not None:
                    fire_ids.append(fire_id)
                if channel_id is None:
                    continue
                by_id = {sub.id: sub for sub in subscriptions.get(trigger.id, [])}
                interactive = bool(getattr(trigger.config, "interactive", True))
                for created, sub_id in _artefacts(result):
                    # A conversation maps to exactly ONE session, so only the
                    # first session a fire starts or feeds binds the thread. An
                    # unthreaded room post has no conversation to bind at all.
                    maps_conversation = mapped_session_id is None and anchor is not None
                    sub = by_id.get(sub_id) if sub_id is not None else None
                    binding = outbound_binding(
                        getattr(sub, "reply_target", None),
                        channel_id=channel_id,
                        thread_anchor=event.thread_anchor,
                        interactive=interactive,
                        maps_conversation=maps_conversation,
                        subscription_id=sub_id,
                    )
                    if not maps_conversation and binding is None:
                        continue
                    await self._map_thread(
                        channel_id=channel_id,
                        anchor=anchor if maps_conversation else None,
                        binding=binding,
                        session_id=created,
                        interactive=interactive,
                    )
                    if maps_conversation:
                        mapped_session_id = created
        if fire_ids:
            return ChannelRouteOutcome(
                kind="fired",
                session_id=mapped_session_id,
                fire_ids=fire_ids,
            )
        return ChannelRouteOutcome(kind="ignored")

    async def _map_thread(
        self,
        *,
        channel_id: str,
        anchor: str | None,
        binding: dict | None,
        session_id: str,
        interactive: bool,
    ) -> None:
        """Bind the conversation and the replies of a session the fire started or fed.

        With ``anchor`` it writes the inbound index (anchor -> session), so the
        next message in the conversation goes to this session. With
        ``binding`` (from :func:`outbound_binding`) it writes the outbound one,
        the session's reply binding in ``session.metadata``. For a binding
        with no ``reply_target`` both name the same conversation, so the 1:1
        holds from both directions; a ``reply_target`` moves only the replies.

        ``anchor`` is the MAPPING key from :func:`mapping_anchor` and may be
        a synthetic ``dm:<sender>``; the binding's anchor is the real platform
        thread id (None in a DM, where the channel root IS the conversation),
        so the outbound side never tries to resolve a synthetic key as a
        thread.

        An interactive trigger relays every turn (S6 section 4); a quiet
        binding (a non-interactive trigger) posts no answers, and
        ``_post_lifecycle`` already no-ops on it.
        """
        from primer.model.workspace_session import WorkspaceSession

        sessions = self._sp.get_storage(WorkspaceSession)
        row = await sessions.get(session_id)
        if row is None:
            return
        if anchor is not None:
            await self._correlation.upsert_thread_session(
                channel_id=channel_id,
                anchor=anchor,
                workspace_id=row.workspace_id,
                session_id=session_id,
            )
        if binding is None:
            return
        # Two metadata leaves, never the whole row read above: the session is live (a fresh one is being claimed, a fed one may be
        # in a turn), and a whole-document write from that read would undo what its worker wrote since.
        leaves = {("metadata", SESSION_REPLY_BINDING_KEY): binding}
        if interactive:
            leaves[("metadata", RELAY_EVERY_TURN_KEY)] = True
        await sessions.patch_if(session_id, None, set_paths=leaves, where={"workspace_id": [row.workspace_id]})

    async def _room_starts_sessions(self, channel_id: str | None, channel: Channel | None) -> bool:
        """Whether a message in this room may start a session: the room's ``chats.enabled`` (ticket 01a16e37-b98e, ruling 4).

        Read from the stored row, not the adapter's copy: the system ``update_channel`` tool changes the row without rebuilding the
        adapter, so the copy can be stale. A room with no row to read (``channel`` alone, or nothing) answers from what it has; an
        unknown room starts sessions as before.
        """
        row = None
        if channel_id is not None:
            try:
                row = await self._sp.get_storage(Channel).get(channel_id)
            except Exception:  # noqa: BLE001 - fall back to the adapter's copy rather than fail the route
                logger.warning("channel event: channel %s could not be read; using the adapter's copy", channel_id, exc_info=True)
        row = row if row is not None else channel
        if row is None:
            return True
        chats = getattr(getattr(row, "config", None), "chats", None)
        return bool(getattr(chats, "enabled", False))

    async def _steer_text(
        self,
        *,
        event: ChannelEvent,
        media_parts: list | None,
        workspace_ids: list[str | None],
        fire_id: str,
    ) -> str:
        """Land any attachments in each named workspace, then build the text the session receives."""
        from primer.channel.media_in import (
            compose_steer_text,
            land_media_in_workspace,
            resolve_active_stt,
            transcribe_voice_parts,
        )

        if not media_parts:
            return event.text or ""
        registry = self._fire_deps.workspace_registry
        store = await self._artifact_store()
        paths: list[str] = []
        for workspace_id in dict.fromkeys(w for w in workspace_ids if w):
            workspace = None
            if registry is not None:
                try:
                    workspace = await registry.get_workspace(workspace_id)
                except Exception:  # noqa: BLE001 - land nothing rather than fail
                    logger.warning(
                        "channel media: workspace %s unreachable", workspace_id,
                        exc_info=True,
                    )
            if workspace is not None:
                landed = await land_media_in_workspace(
                    workspace=workspace,
                    fire_id=fire_id,
                    parts=media_parts,
                    artifact_storage=store,
                )
                paths = paths or landed  # every workspace gets the same media/<fire_id>/ paths
        stt = await resolve_active_stt(self._sp)
        transcript, note = await transcribe_voice_parts(
            parts=media_parts, artifact_storage=store, stt=stt,
        )
        return compose_steer_text(
            event.text, paths, transcript=transcript, note=note,
        )

    async def _artifact_store(self):
        """Resolve the ArtifactStorage behind the injected registry, or None.

        The adapters carry an artifact REGISTRY (``get_default()``), not a
        store (``get(artifact_id)``); the same two-step the platform adapters
        already do before ``store_inbound_media``
        (`primer/channel/slack/adapter.py:282-285`). Passing the registry
        straight through would make every artifact-backed part unresolvable.
        """
        if self._artifacts is None:
            return None
        try:
            return await self._artifacts.get_default()
        except Exception:  # noqa: BLE001 - no store means inline bytes only
            logger.warning(
                "channel media: artifact store unavailable", exc_info=True,
            )
            return None

    async def _resolve_channel_triggers(
        self, provider_id: str, channel_id: str | None,
    ) -> list[Trigger]:
        """Page ``kind="channel"`` triggers for *provider_id*, keeping those
        whose ``channel_id`` is unset (provider-wide) or equal to *channel_id*."""
        storage = self._sp.get_storage(Trigger)
        # The provider is filtered in the query: decoding every channel trigger of every provider per inbound message
        # was the cost (W56i).
        q = Q(Trigger).where_op("config.kind", Op.EQ, "channel").where_op("config.provider_id", Op.EQ, provider_id)
        matched: list[Trigger] = []
        offset = 0
        while offset < 10_000:
            page = await storage.find(
                q.build(), OffsetPage(offset=offset, length=200),
            )
            for trigger in page.items:
                cfg_channel = getattr(trigger.config, "channel_id", None)
                if cfg_channel is None or cfg_channel == channel_id:
                    matched.append(trigger)
            if len(page.items) < 200:
                break
            offset += 200
        return matched


__all__ = ["ChannelEventRouter", "ChannelRouteOutcome", "mapping_anchor"]
