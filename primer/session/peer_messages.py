"""Messages from one session to another: the guards, the rate ledger and the send (pivot P3, design section 6.1).

``workspaces__message_session`` lets the agent in a workspace session hand a message to another session of its workspace (or, from the default
workspace, of any workspace). This module is the service behind it: the tool handlers are thin wrappers over :func:`send_peer_message`. A message
travels the delivery path every machine entrypoint shares (:func:`primer.session.steer_delivery.deliver_steer`): an idle or ended session starts a
turn with it, a session with a turn open reads it at its next turn boundary, and a gate the session waits on is never answered by it.
``workspaces__list_my_sessions`` is the other half: the sessions of the caller's own workspace, in the shape :func:`session_summary` gives
(:func:`list_workspace_session_summaries`), so a sender can pick a target. ``workspaces__steer_workspace_session`` and
``workspaces__restart_workspace_session`` called from inside a session are messages from it too and come through here, with every guard below (pivot
P3 lead ruling 3; lead ruling 1 of the G7 fixes): a restart is delivered by ``restart_session`` instead of ``deliver_steer``.

What a sender may do is decided here, before anything is written. The guards run in this order, and the first one that fails refuses the send with a
:class:`PeerMessageRefused` that carries the tool's error ``type`` and writes nothing (no ledger row, no queued message, no record, no event). Before
them all, a text that is empty or longer than ``PEER_MESSAGE_MAX_CHARS`` and a ``mode`` that is neither ``auto`` nor ``if_idle`` are refused
(``bad-request``): the tool's argument model rejects them first, and this keeps the service safe for any other caller.

1. the target is not the sender (``bad-request``);
2. the sender is a session (``unsupported-surface``);
3. the target is within reach: it is in the sender's workspace, or the sender is in the default workspace. Out of reach is ``not-found`` in the same
   words as a session that does not exist, so a session id seen in a shared file or a log does not confirm that the session exists;
4. the sender ranks at least as high as the target's creator (``forbidden``): the target runs at its creator's rank, so a message drives a run at
   that rank. The rule is :func:`primer.authz.principal_rank`, the one a trigger's steer uses. A sender with no principal cannot be ranked and is
   refused in its own words;
5. the chain of peer messages is shorter than ``MAX_PEER_HOPS`` (``hop-limit``);
6. the sender has sent fewer than ``PEER_RATE_LIMIT`` messages in the last ``PEER_RATE_WINDOW`` (``rate-limited``);
7. with ``mode="if_idle"``, the target would start a turn now (``conflict``);
8. the target can take a message at all (``conflict``), which the delivery decides itself: an ended session that cannot be reopened refuses, and so
   does one an operator cancelled (only a person reopens it: lead ruling R4), and so does a target whose queue already holds the cap's worth of
   waiting messages (a peer message never takes a person's place in it: lead ruling R1).
"""

from __future__ import annotations

import asyncio
import logging
import math
import weakref
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel

from primer.agent.invoke import MAX_INVOCATION_DEPTH
from primer.authz import may_drive, principal_rank, rank_label
from primer.bootstrap.prime import resolve_default_workspace_id
from primer.events.recorder import actor_of, recorder_for
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.principal import PrincipalRef
from primer.model.storage import FieldRef, OffsetPage, Op, OrderBy, Predicate, Value
from primer.model.workspace_session import (
    GraphSessionBinding,
    PeerAttribution,
    PeerMessage,
    SessionState,
    SessionStatus,
    WorkspaceSession,
    session_state_predicate,
)
from primer.session.reset import SessionResetDeps, restart_session
from primer.session.steer_delivery import (
    DELIVERED_MISSING,
    DELIVERED_QUEUE_FULL,
    DELIVERED_QUEUED,
    DELIVERED_SKIPPED_BUSY,
    DELIVERED_WOKEN,
    SteerDelivery,
    deliver_steer,
)
from primer.session.steer_routing import ROUTE_PENDING, route_steer

logger = logging.getLogger(__name__)

#: The longest chain of peer messages. A message a person, a trigger or a channel starts has hop 0; a peer message carries its sender's hop count plus
#: one. A send is refused when the sender's own count is already at the limit, the comparison ``invocation_depth_guard`` makes, so delivered counts run
#: from 1 to ``MAX_PEER_HOPS``. One bound for delegation and for messaging, tuned in one place (``PRIMER_MAX_INVOCATION_DEPTH``).
MAX_PEER_HOPS: int = MAX_INVOCATION_DEPTH
#: A sender may send this many messages in any ``PEER_RATE_WINDOW``. The ledger keeps exactly this many rows per sender.
PEER_RATE_LIMIT: int = 20
PEER_RATE_WINDOW: timedelta = timedelta(minutes=10)
#: The longest message text. Larger outputs go in a file; the tool description says so.
PEER_MESSAGE_MAX_CHARS: int = 32_000

PEER_PREFIX_AGENT = "[from session {session_id} (agent {agent_id})]"
PEER_PREFIX_GRAPH = "[from session {session_id} (graph {graph_id})]"

#: What a parked session waits on, by the prefix of its park key (the text before the first ``:``). A prefix not listed is named by itself.
PARKED_ON_LABELS: dict[str, str] = {
    "ask_user": "ask_user",
    "tool_approval": "approval",
    "evwait": "wait_for_event",
    "timer": "sleep",
    "watch": "watch_files",
    "trigger": "subscribe_to_trigger",
    "external_tool": "external_tool",
    "mcp_task": "mcp_task",
    "tool_wait": "tool_call",
}

REFUSE_HOPS = (
    "this turn was started by a peer message that is already {hops} hops from a person (the limit is {limit}); "
    "answer without messaging another session"
)
REFUSE_RATE = "this session sent {limit} messages in the last 10 minutes; retry in {seconds} seconds"

# What the sending agent reads when a send is refused (the tool answers with the error type named beside each).
REFUSE_SELF = "a session cannot message itself"  # bad-request
REFUSE_NO_CALLER = "message_session works only inside a workspace session"  # unsupported-surface
REFUSE_NOT_FOUND = "Session {session_id!r} does not exist in this workspace"  # not-found
REFUSE_RANK = (
    "session {session_id!r} runs at a higher rank ({target_rank}) than this session ({sender_rank}); "
    "a session may message only sessions at or below its own rank"
)  # forbidden
# Not in the plan: a sender with no principal cannot be ranked at all, and REFUSE_RANK would call it "lower" than a target it was never compared with.
REFUSE_NO_PRINCIPAL = "this session's run has no recorded principal, so it cannot message another session"  # forbidden
REFUSE_BUSY = "session {session_id!r} is {reason}; nothing was sent (mode if_idle)"  # conflict
REFUSE_ENDED = "session {session_id!r} has ended ({ended_reason}) and cannot take a message"  # conflict
# Not in the plan (lead ruling R1 on #788): the target's queue of waiting messages is at its cap, and a peer message never takes a person's place in it.
REFUSE_QUEUE_FULL = "session {session_id!r} is busy and its queue of waiting messages is full; nothing was sent, retry later"  # conflict
# The tool's argument model rejects these first; they keep the service safe for any other caller (an empty text would re-arm a session with nothing).
REFUSE_TEXT = "a message is 1 to {limit} characters; write larger outputs to a file and send its path"  # bad-request
# A restart from inside a session (``workspaces__restart_workspace_session``, lead ruling 1 of the G7 fixes) is a message from that session, so it
# has a text: the one the reopened session starts its new turn with. The first is the tool's own refusal of a call with no ``input``; the second is
# the restart of a session that has not ended, in the words ``reset_session`` uses for it.
REFUSE_RESTART_NEEDS_INPUT = (
    "from inside a workspace session a restart is a message from that session: give input (1 to {limit} characters), "
    "the text the reopened session starts its new turn with"
)  # bad-request
REFUSE_NOT_ENDED = "Session {session_id!r} is not ENDED (status {status}); reset only re-opens ended sessions."  # conflict
REFUSE_MODE = "mode is auto or if_idle, not {mode!r}"  # bad-request

# What the sending agent reads when a send was delivered, by what became of the message.
NOTE_WOKEN = "Delivered: the session starts its next turn with your message."
NOTE_REOPENED = "Delivered: the session had ended; it reopens and starts a new turn with your message."
NOTE_QUEUED_BUSY = "Queued: the session is in a turn; it reads your message when that turn ends."
NOTE_QUEUED_GATE = (
    "Queued: the session is waiting on {parked_on}; your message does not answer it, and the session reads it after that wait and its turn end."
)
NOTE_QUEUED_EVENT = (
    "Queued: the session is waiting for an event. If it waits for session.messaged, that wait ends now; it reads your message when its turn ends."
)
NOTE_QUEUED_PAUSED = "Queued: the session is paused; it reads your message after it is resumed."
NOTE_GRAPH = " This restarts the graph: your message is the input of its next run."
NOTE_GRAPH_STORED_INPUT = (
    " This graph session runs on its stored graph_input, so its next run uses that input and your message is only recorded."
)


class PeerMessageRefused(Exception):
    """A peer message was refused.

    ``error_type`` is the ``type`` the tool answers with (``forbidden``, ``not-found``, ``hop-limit``...), ``message`` the text the sending agent
    reads (``str(exc)`` is the same text). A refusal writes nothing.
    """

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message


def sender_may_message(sender: PrincipalRef | None, target_creator: PrincipalRef | None) -> bool:
    """Whether a run started by ``sender`` may put a message into a session its creator ``target_creator`` started.

    The target session runs at its creator's rank, so the message drives a run at that rank: the sender must rank at least as high
    (:func:`primer.authz.principal_rank`; a target with no recorded creator runs as an unattributed user and ranks as one). A sender with no
    principal at all is refused: an unknown run does not get to drive anyone's (fail closed).
    """
    if sender is None:
        return False
    return may_drive(sender, target_creator)


def next_peer_hops(sender: WorkspaceSession) -> int:
    """The hop count of a message ``sender`` sends now: one more than the message that started its current turn.

    ``sender.peer_hops`` is the hop count of the peer message that started the sender's turn, and 0 when a person, a trigger or a channel did
    (``wake_session`` writes it). A sender already at ``MAX_PEER_HOPS`` is refused: a reply chain between two sessions ends here instead of
    running until the rate limit stops it.
    """
    if sender.peer_hops >= MAX_PEER_HOPS:
        raise PeerMessageRefused("hop-limit", REFUSE_HOPS.format(hops=sender.peer_hops, limit=MAX_PEER_HOPS))
    return sender.peer_hops + 1


# The ledger is read newest first. ``id`` breaks a tie between two rows of the same instant, the same way on every read, so the rows the check
# counts are the rows the trim keeps.
_NEWEST_FIRST = [OrderBy(field="sent_at", direction="desc"), OrderBy(field="id", direction="desc")]


def _sent_by(sender_session_id: str) -> Predicate:
    return Predicate(left=FieldRef(name="from_session_id"), op=Op.EQ, right=Value(value=sender_session_id))


def _as_utc(moment: datetime) -> datetime:
    """``moment`` in UTC. A time with no zone is read as UTC: the ledger is always written in UTC.

    The storage orders a datetime by its text, so every row must carry the same zone for "newest" to mean newest.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


async def check_peer_rate(storage_provider: Any, sender_session_id: str, *, now: datetime) -> None:
    """Refuse (``rate-limited``) when ``sender_session_id`` has already sent ``PEER_RATE_LIMIT`` messages in the last ``PEER_RATE_WINDOW``.

    Reads the sender's newest ``PEER_RATE_LIMIT`` ledger rows: an equality on the sender plus the order, no range predicate on the time (the
    Postgres translator does not type one). The limit is hit when all of them are inside the window; a row exactly one window old has left it.
    The retry time is when the oldest of them leaves the window, rounded up to a whole second, at least one.

    A check is not a reservation: the caller holds the sender's lock from the check until the message is recorded
    (:func:`record_peer_message`), so two sends of one sender cannot both pass at the limit.
    """
    page = await storage_provider.get_storage(PeerMessage).find(
        _sent_by(sender_session_id), OffsetPage(offset=0, length=PEER_RATE_LIMIT), order_by=_NEWEST_FIRST,
    )
    sent = [_as_utc(row.sent_at) for row in page.items]
    if len(sent) < PEER_RATE_LIMIT:
        return
    now = _as_utc(now)
    window_start = now - PEER_RATE_WINDOW
    if not all(moment > window_start for moment in sent):
        return
    seconds = max(1, math.ceil((min(sent) + PEER_RATE_WINDOW - now).total_seconds()))
    raise PeerMessageRefused("rate-limited", REFUSE_RATE.format(limit=PEER_RATE_LIMIT, seconds=seconds))


async def record_peer_message(storage_provider: Any, message: PeerMessage) -> None:
    """Write the ledger row of one delivered message, then drop the sender's rows beyond its newest ``PEER_RATE_LIMIT``.

    The ledger therefore never holds more than ``PEER_RATE_LIMIT`` rows per sender, however long the session lives. Only a delivery is recorded:
    a refused attempt writes nothing, so a refusal does not spend the sender's allowance. The trim is idempotent (a row that is already gone is
    skipped), and the rows it finds are the ones :func:`check_peer_rate` does not count.
    """
    storage = storage_provider.get_storage(PeerMessage)
    await storage.create(message)
    stale = await storage.find(
        _sent_by(message.from_session_id), OffsetPage(offset=PEER_RATE_LIMIT, length=200), order_by=_NEWEST_FIRST,
    )
    for row in stale.items:
        try:
            await storage.delete(row.id)
        except NotFoundError:
            logger.debug("peer ledger: row %s was already gone", row.id)


def parked_on(row: WorkspaceSession) -> str | None:
    """What ``row`` is waiting for, in the tool's words (``ask_user``, ``approval``, ``wait_for_event``, ``sleep``...), or None when it is not parked.

    The answer is read from the park key: ``{kind}:{session}:{call}``, ``kind`` being the text before the first ``:``. A park on several events
    reports its first. An ENDED session waits for nothing: Cancel and the workspace-lost reconcile end a session and leave its park columns set,
    and a label for them would contradict the state the row reads. A park with no key at all reads ``unknown``.
    """
    if row.parked_status is None or row.status == SessionStatus.ENDED:
        return None
    key = row.parked_event_key or (row.parked_event_keys or [""])[0]
    kind = key.partition(":")[0]
    if not kind:
        return "unknown"
    return PARKED_ON_LABELS.get(kind, kind)


def peer_prefix(sender: WorkspaceSession) -> str:
    """The line that opens a peer message, so the target can tell it from a person: ``[from session <id> (agent <id>)]``, or ``(graph <id>)``."""
    binding = sender.binding
    if isinstance(binding, GraphSessionBinding):
        return PEER_PREFIX_GRAPH.format(session_id=sender.id, graph_id=binding.graph_id)
    return PEER_PREFIX_AGENT.format(session_id=sender.id, agent_id=binding.agent_id)


def session_summary(row: WorkspaceSession, *, caller_session_id: str) -> dict[str, Any]:
    """One session as ``list_my_sessions`` shows it: who it is, what it is doing, what it waits on, and whether it is the caller.

    ``state`` is the served ``session_state`` (waiting, running, parked or ended) and ``turn_status`` the scheduler's axis under it; ``parked_on``
    names what a parked session waits for (:func:`parked_on`), so the sender can tell a session that waits for a person from one that is only resting.
    ``agent_id`` and ``graph_id`` are exclusive (the other is None). ``created_by`` is the principal that started the session, as its type, id, display
    name and role, and None for a row saved before attribution was recorded. ``last_activity_at`` is the time of the last turn, else the creation
    time: the rule of ``GET /v1/sessions/recent``. ``self`` marks the caller's own session, which a sender must not message.
    """
    binding = row.binding
    graph_id = binding.graph_id if isinstance(binding, GraphSessionBinding) else None
    return {
        "id": row.id,
        "title": row.name,
        "agent_id": None if graph_id is not None else binding.agent_id,
        "graph_id": graph_id,
        "status": row.status.value,
        "state": row.session_state,
        "turn_status": row.turn_status,
        "parked_on": parked_on(row),
        "created_by": (
            row.initiated_by.model_dump(mode="json", include={"type", "id", "display", "role"}) if row.initiated_by is not None else None
        ),
        "parent_session_id": row.parent_session_id,
        "last_activity_at": (row.last_turn_at or row.created_at).isoformat(),
        "self": row.id == caller_session_id,
    }


async def list_workspace_session_summaries(
    storage_provider: Any, *, workspace_id: str, caller_session_id: str, state: SessionState | None, limit: int,
) -> dict[str, Any]:
    """The newest ``limit`` sessions of ``workspace_id`` (all of them, or only those in ``state``) as ``{"workspace_id", "items", "total"}``.

    The workspace is the CALLER's, taken from its tool context by the tool; it is never an argument, so a session cannot list another workspace
    with this. Newest first by creation time, the order of ``find_sessions``. ``total`` counts every matching session, not only the page; a backend that
    cannot count cheaply reports None and ``total`` is then the size of the page.
    """
    predicate = Predicate(left=FieldRef(name="workspace_id"), op=Op.EQ, right=Value(value=workspace_id))
    if state is not None:
        predicate = Predicate(left=predicate, op=Op.AND, right=session_state_predicate(state))
    page = await storage_provider.get_storage(WorkspaceSession).find(
        predicate, OffsetPage(offset=0, length=limit), order_by=[OrderBy(field="created_at", direction="desc")],
    )
    items = [session_summary(row, caller_session_id=caller_session_id) for row in page.items]
    return {"workspace_id": workspace_id, "items": items, "total": page.total if page.total is not None else len(items)}


async def reach_of(storage_provider: Any, workspace_id: str) -> str | None:
    """The workspace a session of ``workspace_id`` may reach, or ``None`` when it may reach every workspace.

    The scope of every path by which a session acts on another session (design 6.1; lead ruling 3 of the G7 fixes): a session reaches its own
    workspace, and a session in the default workspace (prime, the management plane) reaches every workspace. Which workspace is the default is the one
    every module asks for (``primer.bootstrap.prime.resolve_default_workspace_id``); with none named, no session is in it. A person (REST) or an API
    client (MCP), who calls with no session, is not scoped by this.
    """
    default_workspace_id = await resolve_default_workspace_id(storage_provider)
    return None if workspace_id == default_workspace_id else workspace_id


def in_reach(reach: str | None, workspace_id: str) -> bool:
    """Whether a session in ``workspace_id`` is within ``reach`` (:func:`reach_of`). Out of reach is answered exactly as a session that does not
    exist, by every caller, so a session id seen in a shared file or a log is never confirmed."""
    return reach is None or workspace_id == reach


#: The sentence every tool that names a workspace adds to its description: what a session outside prime reaches (:func:`caller_reach`).
REACH_NOTE = (
    "From a session outside the default workspace (prime) only your own workspace is reachable: another one answers type=not-found, as an "
    "unknown id does."
)

#: The workspace a calling session is confined to when its workspace cannot be told: an id no workspace has (an id is never empty), so it reaches
#: nothing (fail closed).
NO_WORKSPACE = ""


async def caller_reach(storage_provider: Any, ctx: Any) -> str | None:
    """The one workspace the caller of a tool may reach, or ``None`` for every workspace (lead ruling 3 of the G7 fixes, audit G7-05).

    The reach of every tool that names a workspace or a session, in every toolset that has one (the workspaces tools, the reply-binding and
    channel-binding tools, the trigger subscription tools): a call from a workspace session reaches its own workspace, and every workspace when that
    is the default one (prime, the management plane), as :func:`reach_of` says. A call with no calling session (``ctx`` None or with no
    ``session_id``: a person over MCP, an API client) keeps its reach over every workspace. A context that names a session but no workspace is
    resolved through the session's row; a session that cannot be found reaches nothing.
    """
    if ctx is None or not getattr(ctx, "session_id", None):
        return None
    workspace_id = getattr(ctx, "workspace_id", None)
    if not workspace_id:
        row = await storage_provider.get_storage(WorkspaceSession).get(ctx.session_id)
        workspace_id = row.workspace_id if row is not None else NO_WORKSPACE
    return await reach_of(storage_provider, workspace_id)


class PeerSendResult(BaseModel):
    """What ``message_session`` answers when the message was delivered or queued."""

    message_id: str
    session_id: str
    workspace_id: str
    delivery: Literal["woken", "queued"]
    hops: int
    target_state: SessionState
    target_parked_on: str | None
    restarts_graph: bool
    note: str


# One lock per sender, held from the rate check until the message is recorded, so the check and the write are one step. A session's turn runs in one
# process (the claim lease), so a process-local lock is exact. Weakly held: a sender that is not sending has no lock.
_SENDER_LOCKS: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()


def _sender_lock(sender_session_id: str) -> asyncio.Lock:
    lock = _SENDER_LOCKS.get(sender_session_id)
    if lock is None:
        lock = asyncio.Lock()
        _SENDER_LOCKS[sender_session_id] = lock
    return lock


def _is_paused(row: WorkspaceSession) -> bool:
    """Whether ``row`` is paused on purpose, whether the pause landed while it ran (``pause_requested``) or while it was idle (status PAUSED).

    An ENDED session is never paused: reopening it clears the flag.
    """
    return row.status != SessionStatus.ENDED and (row.status == SessionStatus.PAUSED or row.pause_requested)


def _busy_reason(row: WorkspaceSession) -> str | None:
    """Why ``row`` would not start a turn with a message right now (``busy``, ``waiting on <what>`` or ``paused``), or None when it would.

    A session that waits for something is named by that, then a paused one, then one in a turn. An ENDED session would start a turn (it reopens).
    """
    waiting_on = parked_on(row)
    if waiting_on is not None:
        return f"waiting on {waiting_on}"
    if _is_paused(row):
        return "paused"
    if route_steer(row) == ROUTE_PENDING:
        return "busy"
    return None


async def _refusal_for_missing(sessions: Any, target_session_id: str, delivery: SteerDelivery) -> PeerMessageRefused:
    """The refusal for a delivery that reported the session missing, in the words of the wake that said why.

    ``delivery.conflict`` is an ENDED session that cannot take the message: one that cannot be reopened (``force_deleted``, a lost workspace that is
    not back), or one an operator cancelled (a peer message does not reopen it); otherwise the row went away between the guards' read and the delivery,
    or the workspace was removed under the write (``delivery.detail`` says which).
    """
    if delivery.conflict:
        current = await sessions.get(target_session_id)
        if current is not None and current.status == SessionStatus.ENDED:
            return PeerMessageRefused(
                "conflict", REFUSE_ENDED.format(session_id=target_session_id, ended_reason=current.ended_reason),
            )
        return PeerMessageRefused("conflict", delivery.detail or f"session {target_session_id!r} cannot take a message")
    return PeerMessageRefused("not-found", delivery.detail or REFUSE_NOT_FOUND.format(session_id=target_session_id))


async def _deliver_restart(
    *,
    storage_provider: Any,
    scheduler: Any,
    claim_engine: Any,
    workspace_registry: Any,
    event_bus: Any,
    target: WorkspaceSession,
    text: str,
    peer: PeerAttribution,
) -> SteerDelivery:
    """Restart the ENDED session ``target`` with ``text`` as a message from another session, reported the way ``deliver_steer`` reports a delivery.

    The restart is ``restart_session``'s, the one the REST route and the tool always ran: ``reset_session`` reopens the row under the session's
    lifecycle lock (refusing, on the row read there, one that has not ended, one that cannot be reopened, and a ``cancelled`` one: no person stands
    behind this restart), then ``wake_session`` writes the USER_INPUT with ``peer`` and arms the turn. A refusal is ``DELIVERED_MISSING`` with the
    reason, as the steer path reports a session that cannot take a message.
    """
    from primer.session.enqueue import SessionWakeDeps

    try:
        await restart_session(
            workspace_id=target.workspace_id,
            session_id=target.id,
            instruction=text,
            human_intent=False,
            peer=peer,
            reset_deps=SessionResetDeps(
                storage_provider=storage_provider, workspace_registry=workspace_registry, event_bus=event_bus,
            ),
            wake_deps=SessionWakeDeps(
                storage_provider=storage_provider,
                scheduler=scheduler,
                claim_engine=claim_engine,
                workspace_registry=workspace_registry,
                event_bus=event_bus,
            ),
        )
    except ConflictError as exc:
        return SteerDelivery(outcome=DELIVERED_MISSING, detail=exc.message, conflict=True)
    except NotFoundError as exc:
        return SteerDelivery(outcome=DELIVERED_MISSING, detail=exc.message)
    return SteerDelivery(outcome=DELIVERED_WOKEN, session_id=target.id)


async def send_peer_message(
    *,
    storage_provider: Any,
    scheduler: Any,
    claim_engine: Any,
    workspace_registry: Any,
    event_bus: Any,
    sender_session_id: str,
    sender_principal: PrincipalRef | None,
    target_session_id: str,
    text: str,
    mode: Literal["auto", "if_idle"] = "auto",
    now: datetime | None = None,
    steered_on_every_delivery: bool = False,
    restart: bool = False,
) -> PeerSendResult:
    """Deliver ``text`` from the session ``sender_session_id`` to the session ``target_session_id``, or raise :class:`PeerMessageRefused`.

    The guards run in the order of the module docstring and each refusal writes nothing. A delivery goes through
    :func:`~primer.session.steer_delivery.deliver_steer`, the path every machine entrypoint shares, as a message no person stands behind
    (``human_intent=False``): a session paused on purpose stays paused and queues it, a session with a turn open reads it at its next turn
    boundary, and a gate the session waits on is never answered by it. ``mode="auto"`` delivers by the target's state; ``mode="if_idle"`` only
    when the target would start a turn now.

    The rate check, the delivery and the ledger write are one step under the sender's lock, so concurrent sends of one sender cannot all pass at
    the limit. The state the guards and the result describe (is a turn open, what the session waits on, is it paused) is read under the lock,
    just before the delivery; the delivery reads the row once more and is what decides, so a session that changed in that instant is delivered
    to by its new state and the result says what happened (``delivery``). For ``mode="if_idle"`` that means: a turn that opens in that instant is
    caught by the delivery (``skip``, refused, nothing stored), but a pause that lands in it holds the message like any other non-human wake, and
    the result says ``queued``.

    The delivery also refuses, with nothing written (``conflict``), a target an operator cancelled and a target whose queue of waiting messages is full.
    The first is decided by the wake under the session's lifecycle lock, on the row read there, so a Cancel that lands after the guards above is seen
    (only a person reopens a cancelled session). The second is decided by the queue when it stores the message, for both ways a message is queued (behind
    a turn or a park, and behind a pause), and is the busy answer: the sender may retry. Two senders can still pass that check together; the queue's cap
    then drops the oldest PEER message, never a person's steer.

    Once the message is delivered the call succeeds: a ledger row that cannot be written is logged, not raised (the sender would send the message
    again), at the price of one delivered message the rate limit does not count.

    Two events follow a delivery: ``session.messaged`` always (it carries the attribution, never the text, so a session waiting on it for
    ``wait_for_event`` is woken as by a doorbell while the message itself stays queued for the turn boundary), and ``session.steered`` too when the
    target had a turn open.

    One switch exists for ``steer_workspace_session`` called from inside a session, which is a peer message too (lead ruling 3) and the only caller
    that sets it: ``steered_on_every_delivery`` emits ``session.steered`` for a woken target as well as a queued one, as that tool always has (the
    REST steer route emits it for every steer). Every guard, the reach of the sender's workspace included (lead ruling 3 of the G7 fixes: it used to
    skip it), the ledger and the rulings R1 and R4 apply as for ``message_session``.

    ``restart`` is ``restart_workspace_session`` called from inside a session (lead ruling 1 of the G7 fixes): a restart is a message from the
    calling session too, so every guard above applies to it, and its text reaches the reopened session with the prefix and the attribution, and its
    turn with the hop count. It only reopens an ENDED session: one that has not ended is refused (``conflict``, decided on the row read under the
    sender's lock and again by ``reset_session`` under the session's), and so is one an operator cancelled, as for any peer message. The delivery is
    ``restart_session``'s (a reset, then a wake), so the answer is always ``woken``.
    """
    if not text or len(text) > PEER_MESSAGE_MAX_CHARS:
        raise PeerMessageRefused("bad-request", REFUSE_TEXT.format(limit=PEER_MESSAGE_MAX_CHARS))
    if mode not in ("auto", "if_idle"):
        raise PeerMessageRefused("bad-request", REFUSE_MODE.format(mode=mode))
    if target_session_id == sender_session_id:
        raise PeerMessageRefused("bad-request", REFUSE_SELF)
    sessions = storage_provider.get_storage(WorkspaceSession)
    sender = await sessions.get(sender_session_id)
    if sender is None:
        raise PeerMessageRefused("unsupported-surface", REFUSE_NO_CALLER)
    target = await sessions.get(target_session_id)
    # A session reaches its own workspace; the default workspace (prime) is the management plane, and its sessions reach ANY workspace (design 6.1,
    # :func:`reach_of`). For every caller of this service, the steer and restart tools included.
    if target is None or not in_reach(await reach_of(storage_provider, sender.workspace_id), target.workspace_id):
        # A session of another workspace is answered exactly as a session that does not exist: an id seen in a shared file or a log must not confirm
        # that the session is there. This comes before the rank check for the same reason.
        raise PeerMessageRefused("not-found", REFUSE_NOT_FOUND.format(session_id=target_session_id))
    if sender_principal is None:
        raise PeerMessageRefused("forbidden", REFUSE_NO_PRINCIPAL)
    if not sender_may_message(sender_principal, target.initiated_by):
        raise PeerMessageRefused(
            "forbidden",
            REFUSE_RANK.format(
                session_id=target.id, target_rank=rank_label(target.initiated_by), sender_rank=rank_label(sender_principal),
            ),
        )
    hops = next_peer_hops(sender)

    async with _sender_lock(sender.id):
        # Taken here, not before the wait for the lock: a send that waited behind another send of the same sender is recorded when it was sent, and the
        # rate window counts from the time on the row.
        sent_at = _as_utc(now) if now is not None else datetime.now(UTC)
        await check_peer_rate(storage_provider, sender.id, now=sent_at)
        # Reach and rank were decided from the first read (its workspace and its creator never change). What STATE the target is in is read again
        # here, after the lock wait and the rate check, so the if_idle guard, the result, the note and the events describe the row the delivery is
        # about to look at, not one from before another sender's message woke it.
        target = await sessions.get(target.id)
        if target is None:
            raise PeerMessageRefused("not-found", REFUSE_NOT_FOUND.format(session_id=target_session_id))
        if restart and target.status != SessionStatus.ENDED:
            raise PeerMessageRefused("conflict", REFUSE_NOT_ENDED.format(session_id=target.id, status=target.status.value))
        if mode == "if_idle":
            busy = _busy_reason(target)
            if busy is not None:
                raise PeerMessageRefused("conflict", REFUSE_BUSY.format(session_id=target.id, reason=busy))

        message = PeerMessage(
            from_session_id=sender.id,
            from_workspace_id=sender.workspace_id,
            to_session_id=target.id,
            to_workspace_id=target.workspace_id,
            hops=hops,
            delivery="woken",  # placeholder: the ledger row is written with the real delivery below, and keeps this id
            sent_at=sent_at,
        )
        sender_graph = sender.binding.graph_id if isinstance(sender.binding, GraphSessionBinding) else None
        attribution = PeerAttribution(
            from_session_id=sender.id,
            from_workspace_id=sender.workspace_id,
            from_agent_id=None if sender_graph is not None else sender.binding.agent_id,
            from_graph_id=sender_graph,
            hops=hops,
            message_id=message.id,
        )
        graph_target = isinstance(target.binding, GraphSessionBinding)
        # A graph takes the message as its input, and a prefix would become part of it; the sender is on the record (``payload["peer"]``).
        body = text if graph_target else f"{peer_prefix(sender)}\n{text}"
        is_steer = route_steer(target) == ROUTE_PENDING
        target_state = target.session_state
        waiting_on = parked_on(target)
        paused = _is_paused(target)

        if restart:
            delivery = await _deliver_restart(
                storage_provider=storage_provider,
                scheduler=scheduler,
                claim_engine=claim_engine,
                workspace_registry=workspace_registry,
                event_bus=event_bus,
                target=target,
                text=body,
                peer=attribution,
            )
        else:
            delivery = await deliver_steer(
                session_id=target.id,
                text=body,
                parallelism="queue" if mode == "auto" else "skip",
                human_intent=False,
                peer=attribution,
                storage_provider=storage_provider,
                scheduler=scheduler,
                claim_engine=claim_engine,
                workspace_registry=workspace_registry,
                event_bus=event_bus,
            )
        if delivery.outcome == DELIVERED_SKIPPED_BUSY:
            # The target had a turn open by the time the delivery looked (the guards saw it idle): nothing was stored.
            raise PeerMessageRefused("conflict", REFUSE_BUSY.format(session_id=target.id, reason="busy"))
        if delivery.outcome == DELIVERED_QUEUE_FULL:
            # The target's queue of waiting messages is full (checked by the queue itself, under the delivery, for both ways a message is queued):
            # nothing was stored, so no ledger row is written and the allowance is not spent.
            raise PeerMessageRefused("conflict", REFUSE_QUEUE_FULL.format(session_id=target.id))
        if delivery.outcome == DELIVERED_MISSING:
            raise await _refusal_for_missing(sessions, target.id, delivery)
        how: Literal["woken", "queued"] = "queued" if delivery.outcome == DELIVERED_QUEUED else "woken"
        try:
            await record_peer_message(storage_provider, message.model_copy(update={"delivery": how}))
        except Exception:  # noqa: BLE001 -- the message is delivered; see below
            # The message HAS been delivered. Failing the call now would make the sender send it again and the target read it twice; the price of
            # answering is one delivered message the rate limit does not count. Loud, not silent.
            logger.exception(
                "peer ledger: message %s from session %s to session %s was delivered but its ledger row was not written",
                message.id, sender.id, target.id,
            )

    recorder = recorder_for(storage_provider, event_bus)
    actor = actor_of(sender_principal)
    await recorder.emit(
        "session.messaged",
        actor=actor,
        workspace_id=target.workspace_id,
        session_id=target.id,
        correlation_id=message.id,
        payload={
            "message_id": message.id,
            "from_session_id": sender.id,
            "from_workspace_id": sender.workspace_id,
            "from_agent_id": attribution.from_agent_id,
            "from_graph_id": attribution.from_graph_id,
            "to_session_id": target.id,
            "to_workspace_id": target.workspace_id,
            "hops": hops,
            "mode": mode,
            "delivery": how,
        },
    )
    if is_steer or steered_on_every_delivery:
        await recorder.emit(
            "session.steered",
            actor=actor,
            workspace_id=target.workspace_id,
            session_id=target.id,
            correlation_id=message.id,
            payload={
                "has_instruction": True,
                "has_tool_results": False,
                "has_attachments": False,
                "from_session_id": sender.id,
                "delivery": how,
            },
        )

    if how == "woken":
        note = NOTE_REOPENED if target.status == SessionStatus.ENDED else NOTE_WOKEN
    elif waiting_on == "wait_for_event":
        note = NOTE_QUEUED_EVENT
    elif waiting_on is not None:
        note = NOTE_QUEUED_GATE.format(parked_on=waiting_on)
    elif paused:
        note = NOTE_QUEUED_PAUSED
    else:
        note = NOTE_QUEUED_BUSY
    if graph_target:
        note += NOTE_GRAPH
        if "graph_input" in target.metadata:
            note += NOTE_GRAPH_STORED_INPUT
    logger.info(
        "peer message %s: session %s -> session %s (%s, hop %d, mode %s)", message.id, sender.id, target.id, how, hops, mode,
    )
    return PeerSendResult(
        message_id=message.id,
        session_id=target.id,
        workspace_id=target.workspace_id,
        delivery=how,
        hops=hops,
        target_state=target_state,
        target_parked_on=waiting_on,
        restarts_graph=graph_target,
        note=note,
    )


__all__ = [
    "MAX_PEER_HOPS",
    "NO_WORKSPACE",
    "REACH_NOTE",
    "PARKED_ON_LABELS",
    "PEER_MESSAGE_MAX_CHARS",
    "PEER_PREFIX_AGENT",
    "PEER_PREFIX_GRAPH",
    "PEER_RATE_LIMIT",
    "PEER_RATE_WINDOW",
    "NOTE_GRAPH",
    "NOTE_GRAPH_STORED_INPUT",
    "NOTE_QUEUED_BUSY",
    "NOTE_QUEUED_EVENT",
    "NOTE_QUEUED_GATE",
    "NOTE_QUEUED_PAUSED",
    "NOTE_REOPENED",
    "NOTE_WOKEN",
    "REFUSE_BUSY",
    "REFUSE_ENDED",
    "REFUSE_HOPS",
    "REFUSE_MODE",
    "REFUSE_NOT_FOUND",
    "REFUSE_NO_CALLER",
    "REFUSE_NOT_ENDED",
    "REFUSE_NO_PRINCIPAL",
    "REFUSE_QUEUE_FULL",
    "REFUSE_RANK",
    "REFUSE_RATE",
    "REFUSE_RESTART_NEEDS_INPUT",
    "REFUSE_SELF",
    "REFUSE_TEXT",
    "PeerMessageRefused",
    "PeerSendResult",
    "caller_reach",
    "check_peer_rate",
    "in_reach",
    "list_workspace_session_summaries",
    "next_peer_hops",
    "parked_on",
    "peer_prefix",
    "reach_of",
    "rank_label",
    "record_peer_message",
    "resolve_default_workspace_id",
    "send_peer_message",
    "sender_may_message",
    "session_summary",
]
