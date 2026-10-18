"""Messages from one session to another: the guards, the rate ledger and the send (pivot P3, design section 6.1).

``workspaces__message_session`` lets the agent in a workspace session hand a message to another session of its workspace (or, from the default
workspace, of any workspace). This module is the service behind it: the tool handlers are thin wrappers over :func:`send_peer_message`. A message
travels the delivery path every machine entrypoint shares (:func:`primer.session.steer_delivery.deliver_steer`): an idle or ended session starts a
turn with it, a session with a turn open reads it at its next turn boundary, and a gate the session waits on is never answered by it.

What a sender may do is decided here, before anything is written. The guards run in this order, and the first one that fails refuses the send with a
:class:`PeerMessageRefused` that carries the tool's error ``type`` and writes nothing (no ledger row, no queued message, no record, no event):

1. the target is not the sender (``bad-request``);
2. the sender is a session (``unsupported-surface``);
3. the target is within reach: it is in the sender's workspace, or the sender is in the default workspace. Out of reach is ``not-found`` in the same
   words as a session that does not exist, so a session id seen in a shared file or a log does not confirm that the session exists;
4. the sender ranks at least as high as the target's creator (``forbidden``): the target runs at its creator's rank, so a message drives a run at
   that rank. The rule is :func:`primer.authz.principal_rank`, the one a trigger's steer uses;
5. the chain of peer messages is shorter than ``MAX_PEER_HOPS`` (``hop-limit``);
6. the sender has sent fewer than ``PEER_RATE_LIMIT`` messages in the last ``PEER_RATE_WINDOW`` (``rate-limited``);
7. with ``mode="if_idle"``, the target would start a turn now (``conflict``);
8. the target can take a message at all: an ended session that cannot be reopened refuses (``conflict``).
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta
from typing import Any

from primer.agent.invoke import MAX_INVOCATION_DEPTH
from primer.authz import principal_rank, rank_label
from primer.bootstrap.defaults import RESERVED_DEFAULT_WORKSPACE
from primer.model.except_ import NotFoundError
from primer.model.principal import PrincipalRef
from primer.model.storage import FieldRef, OffsetPage, Op, OrderBy, Predicate, Value
from primer.model.workspace_session import GraphSessionBinding, PeerMessage, SessionStatus, WorkspaceSession

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
    return principal_rank(sender) >= principal_rank(target_creator)


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
    """A stored time with no zone is read as UTC (the ledger is always written in UTC)."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


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


async def resolve_default_workspace_id(storage_provider: Any) -> str:
    """The id of the default workspace: the one whose sessions may message a session in ANY workspace (design 6.1, the management plane).

    Works before and after pivot P2 adds ``SystemState.default_workspace_id``: until a deployment records one, the reserved id
    (``RESERVED_DEFAULT_WORKSPACE``, ``"primer"``) is the default. If P2 ships its own reader, this body becomes a call to it.
    """
    state = await storage_provider.get_system_state()
    return getattr(state, "default_workspace_id", None) or RESERVED_DEFAULT_WORKSPACE


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


__all__ = [
    "MAX_PEER_HOPS",
    "PARKED_ON_LABELS",
    "PEER_MESSAGE_MAX_CHARS",
    "PEER_PREFIX_AGENT",
    "PEER_PREFIX_GRAPH",
    "PEER_RATE_LIMIT",
    "PEER_RATE_WINDOW",
    "REFUSE_HOPS",
    "REFUSE_RATE",
    "PeerMessageRefused",
    "check_peer_rate",
    "next_peer_hops",
    "parked_on",
    "peer_prefix",
    "rank_label",
    "record_peer_message",
    "resolve_default_workspace_id",
    "sender_may_message",
]
