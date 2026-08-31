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

import logging
from dataclasses import dataclass, field

from primer.channel.correlation import CorrelationStore
from primer.channel.reply_binding import SESSION_REPLY_BINDING_KEY
from primer.int.storage_provider import StorageProvider
from primer.model.channel import Channel
from primer.model.channel_event import ChannelEvent
from primer.model.envelope import RELAY_EVERY_TURN_KEY
from primer.model.storage import Op, OffsetPage
from primer.model.trigger import Trigger
from primer.model.workspace_session import SessionStatus, WorkspaceSession
from primer.session.steer_delivery import DELIVERED_MISSING, deliver_steer
from primer.storage.q import Q
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


def _first_artefact(result) -> str | None:
    """The first session a fire actually created, if any."""
    for envelope in result.results:
        if (
            envelope.get("ok")
            and not envelope.get("skipped")
            and envelope.get("artefact_id")
        ):
            return envelope["artefact_id"]
    return None


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
        """Route one normalized inbound event and report what it did."""
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
        if anchor and channel_id is not None:
            record = await self._correlation.lookup(channel_id, anchor)
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
                    await self._correlation.clear_gate(channel_id, anchor)
                    delivery = await deliver_steer(
                        session_id=record.session_id,
                        text=await self._steer_text(
                            event=event,
                            media_parts=media_parts,
                            workspace_id=target.workspace_id,
                            fire_id=event.event_id,
                        ),
                        parallelism="queue",
                        storage_provider=self._sp,
                        scheduler=self._fire_deps.scheduler,
                        claim_engine=self._fire_deps.claim_engine,
                        workspace_registry=self._fire_deps.workspace_registry,
                        event_bus=self._bus,
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
                    event_key = (
                        f"ask_user:{record.session_id}:{record.tool_call_id}"
                    )
                    await self._bus.publish(event_key, {"response": event.text})
                    # One reply answers one gate. Clearing it here is what
                    # lets the NEXT reply in the same thread steer the
                    # session instead of re-publishing onto a dead resume
                    # key.
                    await self._correlation.clear_gate(channel_id, anchor)
                    return ChannelRouteOutcome(
                        kind="gate", session_id=record.session_id,
                    )
            if record is not None and record.kind == "session":
                # Thread-mapped session, no open gate: the reply is the next
                # user message on that session (S6 section 5). Queueing is
                # the parallelism a human conversation wants; a steer that
                # lands mid-turn is realized at the drain checkpoint.
                delivery = await deliver_steer(
                    session_id=record.session_id,
                    text=await self._steer_text(
                        event=event,
                        media_parts=media_parts,
                        workspace_id=record.workspace_id,
                        fire_id=event.event_id,
                    ),
                    parallelism="queue",
                    storage_provider=self._sp,
                    scheduler=self._fire_deps.scheduler,
                    claim_engine=self._fire_deps.claim_engine,
                    workspace_registry=self._fire_deps.workspace_registry,
                    event_bus=self._bus,
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
        if media_parts:
            # The created session's FIRST instruction must reference the
            # same landed files the steer path composes, so a fresh thread
            # with an attachment is not silently text-only. No workspace is
            # resolvable before _map_thread runs, so the attachments are
            # referenced by name here.
            event_payload["text"] = await self._steer_text(
                event=event,
                media_parts=media_parts,
                workspace_id=None,
                fire_id=event.event_id,
            )
        extra_context = {"event": event_payload}
        fire_ids: list[str] = []
        mapped_session_id: str | None = None
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
            # A conversation maps to exactly ONE session, so only the first
            # created session binds the thread. An unthreaded room post has
            # no conversation to bind at all.
            if mapped_session_id is None and anchor and channel_id is not None:
                created = _first_artefact(result)
                if created is not None:
                    await self._map_thread(
                        channel_id=channel_id,
                        anchor=anchor,
                        reply_anchor=event.thread_anchor,
                        session_id=created,
                        interactive=bool(
                            getattr(trigger.config, "interactive", True)
                        ),
                    )
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
        anchor: str,
        reply_anchor: str | None,
        session_id: str,
        interactive: bool,
    ) -> None:
        """Bind the conversation to the session the fire just created.

        Writes the inbound index (anchor -> session) and the outbound one
        (session.metadata reply binding -> thread), so the 1:1 holds from
        both directions.

        ``anchor`` is the MAPPING key from :func:`mapping_anchor` and may be
        a synthetic ``dm:<sender>``; ``reply_anchor`` is the real platform
        thread id (None in a DM, where the channel root IS the conversation),
        so the outbound side never tries to resolve a synthetic key as a
        thread.

        ``quiet`` carries the trigger's interactive flag inverted: a
        non-interactive channel trigger ingests silently (S6 section 4), and
        ``_post_lifecycle`` already no-ops on a quiet binding.
        """
        from primer.model.workspace_session import WorkspaceSession

        sessions = self._sp.get_storage(WorkspaceSession)
        row = await sessions.get(session_id)
        if row is None:
            return
        await self._correlation.upsert_thread_session(
            channel_id=channel_id,
            anchor=anchor,
            workspace_id=row.workspace_id,
            session_id=session_id,
        )
        metadata = dict(row.metadata or {})
        metadata[SESSION_REPLY_BINDING_KEY] = {
            "channel_id": channel_id,
            "anchor": reply_anchor,
            "quiet": not interactive,
        }
        if interactive:
            metadata[RELAY_EVERY_TURN_KEY] = True
        await sessions.update(row.model_copy(update={"metadata": metadata}))

    async def _steer_text(
        self,
        *,
        event: ChannelEvent,
        media_parts: list | None,
        workspace_id: str | None,
        fire_id: str,
    ) -> str:
        """Land any attachments, then build the text the session receives."""
        from primer.channel.media_in import (
            compose_steer_text,
            land_media_in_workspace,
            resolve_active_stt,
            transcribe_voice_parts,
        )

        if not media_parts:
            return event.text or ""
        registry = self._fire_deps.workspace_registry
        workspace = None
        if registry is not None and workspace_id:
            try:
                workspace = await registry.get_workspace(workspace_id)
            except Exception:  # noqa: BLE001 - land nothing rather than fail
                logger.warning(
                    "channel media: workspace %s unreachable", workspace_id,
                    exc_info=True,
                )
        store = await self._artifact_store()
        paths: list[str] = []
        if workspace is not None:
            paths = await land_media_in_workspace(
                workspace=workspace,
                fire_id=fire_id,
                parts=media_parts,
                artifact_storage=store,
            )
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
        q = Q(Trigger).where_op("config.kind", Op.EQ, "channel")
        matched: list[Trigger] = []
        offset = 0
        while offset < 10_000:
            page = await storage.find(
                q.build(), OffsetPage(offset=offset, length=200),
            )
            for trigger in page.items:
                cfg = trigger.config
                if getattr(cfg, "provider_id", None) != provider_id:
                    continue
                cfg_channel = getattr(cfg, "channel_id", None)
                if cfg_channel is None or cfg_channel == channel_id:
                    matched.append(trigger)
            if len(page.items) < 200:
                break
            offset += 200
        return matched


__all__ = ["ChannelEventRouter", "ChannelRouteOutcome", "mapping_anchor"]
