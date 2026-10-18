"""What ends a ``canvas_wait``, and what it leaves behind (pivot P5, Task 10; plan Decision 8).

An event ends the wait when its element is in ``ids`` (or ``ids`` is empty) and, when ``kinds`` is given, its kind is in ``kinds``;
without ``kinds`` only an event that wakes the agent ends it. The rule exists twice, and a test pins that the two agree:

* :func:`canvas_wait_filter` is the ``EventFilter`` of the one-shot subscription the tool parks on, so the dispatcher wakes the turn for
  exactly the events that end the wait;
* :func:`canvas_wait_matches` is the same rule as a predicate over an event payload, for the events the canvas already holds when the
  tool is called (the ones that arrived since the agent last read it) and for the delivery code that has to say whether a parked wait
  would take an event.

An event that ends a wait is delivered ONCE. The click that ended it may also sit in the session's queue of steers (it arrived while
the turn was running, before the wait began), so :func:`consume_events` drops those rows when it marks the events seen.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from primer.model.event import EventFilter, FieldMatcher
from primer.model.except_ import NotFoundError
from primer.model.storage import FieldRef, OffsetPage, Op, OrderBy, Predicate, Value
from primer.model.workspace_session import PendingSessionMessage
from primer.model.yield_ import ToolContext

logger = logging.getLogger(__name__)

#: The name the tool parks under: the key of its resume hook and the ``kind`` of its entry in a parked session's pending list.
CANVAS_WAIT_TOOL = "canvas_wait"
#: The start of every event key a canvas wait parks on.
CANVAS_WAIT_PREFIX = "canvaswait:"

#: Where a queued canvas click carries its event in ``PendingSessionMessage.attribution`` (``{"canvas_event": {"id": "e3", ...}}``);
#: the delivery code (``primer.canvas.delivery``) writes it with the same key.
_ATTRIBUTION_KEY = "canvas_event"

#: Queued steers are read a page at a time; a session queues at most 50 (``pending_messages``), so one page is the usual case and the
#: page count only bounds a bug.
_PAGE = 200
_MAX_PAGES = 5


def canvas_wait_event_key(ctx: ToolContext, node_id: str | None) -> str:
    """The event key a wait parks on: ``canvaswait:{session}:{call}``, or ``canvaswait:{session}:{node}:{call}`` inside a graph node.

    A provider repeats ``call_0``, ``call_1`` ... in every conversation and two fan-out siblings of one graph superstep can share a raw
    id, so the key carries the session and, in a graph, the node instance (as ``ask_user``'s and ``sleep``'s keys do).
    """
    if node_id is not None:
        return f"{CANVAS_WAIT_PREFIX}{ctx.session_id}:{node_id}:{ctx.tool_call_id}"
    return f"{CANVAS_WAIT_PREFIX}{ctx.session_id}:{ctx.tool_call_id}"


def _any_of(words: Sequence[str]) -> str:
    return "^(" + "|".join(re.escape(word) for word in words) + ")$"


def canvas_wait_filter(session_id: str, *, ids: Sequence[str], kinds: Sequence[str]) -> EventFilter:
    """The subscription filter for a wait: this session's ``canvas.event`` events that end the wait (see the module docstring).

    ``ids`` and ``kinds`` are matched as anchored alternations of literal words (the ids are element ids and the kinds are event kinds,
    so no word holds a regex character; they are escaped anyway). The dispatcher compiles the pattern once and runs it on its own loop.
    """
    fields = [FieldMatcher(path="session_id", op="eq", value=session_id)]
    if ids:
        fields.append(FieldMatcher(path="payload.element", op="regex", value=_any_of(ids)))
    if kinds:
        fields.append(FieldMatcher(path="payload.kind", op="regex", value=_any_of(kinds)))
    else:
        fields.append(FieldMatcher(path="payload.wake", op="eq", value="true"))
    return EventFilter(event_types=["canvas.event"], fields=fields)


def canvas_wait_matches(meta: Mapping[str, Any], event: Mapping[str, Any]) -> bool:
    """Whether ``event`` (the payload of a ``canvas.event``) ends a wait whose ``meta`` holds its ``ids`` and ``kinds``."""
    ids = meta.get("ids") or []
    kinds = meta.get("kinds") or []
    if ids and event.get("element") not in ids:
        return False
    if kinds:
        return event.get("kind") in kinds
    return event.get("wake") is True


def _event_id_of(attribution: Mapping[str, Any] | None) -> str | None:
    found = attribution.get(_ATTRIBUTION_KEY) if isinstance(attribution, Mapping) else None
    event_id = found.get("id") if isinstance(found, Mapping) else None
    return event_id if isinstance(event_id, str) else None


async def drop_queued_steers(storage_provider: Any, session_id: str, event_ids: Iterable[str]) -> int:
    """Delete the session's queued steers that carry one of the canvas events ``event_ids``; return how many.

    A click that lands while a turn is running is queued as a steer (it must not answer whatever the turn is doing), and when that turn
    then calls ``canvas_wait`` the same click ends the wait. The agent has it as the tool's result, so the queued copy would deliver it a
    second time. A row another process has already realized or dropped is skipped.
    """
    wanted = set(event_ids)
    if not wanted:
        return 0
    storage = storage_provider.get_storage(PendingSessionMessage)
    predicate = Predicate(left=FieldRef(name="session_id"), op=Op.EQ, right=Value(value=session_id))
    order = [OrderBy(field="enqueued_at", direction="asc"), OrderBy(field="id", direction="asc")]
    doomed: list[str] = []
    for page_no in range(_MAX_PAGES):
        page = await storage.find(predicate, OffsetPage(offset=page_no * _PAGE, length=_PAGE), order_by=order)
        doomed += [row.id for row in page.items if _event_id_of(row.attribution) in wanted]
        if len(page.items) < _PAGE:
            break
    dropped = 0
    for row_id in doomed:
        try:
            await storage.delete(row_id)
        except NotFoundError:
            continue
        dropped += 1
    return dropped


async def consume_events(service: Any, storage_provider: Any, session_id: str, events: Sequence[Mapping[str, Any]]) -> None:
    """The agent is being handed ``events`` as a wait's result: drop their queued copies, then mark them seen.

    The order is the safe one for a crash in between: a steer dropped and the events not yet marked means the agent reads them again
    in its next ``canvas_get`` or wait; marked first and the steer left would deliver the click twice. Best effort and never raises:
    the tool's result is already decided, and a failed step costs at worst a repeated event.
    """
    ids = [event["id"] for event in events if isinstance(event.get("id"), str)]
    seqs = [event["seq"] for event in events if isinstance(event.get("seq"), int) and not isinstance(event.get("seq"), bool)]
    try:
        await drop_queued_steers(storage_provider, session_id, ids)
    except Exception:  # noqa: BLE001 - the result is decided; a queued copy delivers the click a second time at worst
        logger.warning("canvas_wait: the queued steers of %s could not be dropped for session %s", ids, session_id, exc_info=True)
    if seqs:
        try:
            await service.mark_seen(session_id, max(seqs))
        except Exception:  # noqa: BLE001 - the events are read again at worst
            logger.warning("canvas_wait: the events %s could not be marked seen for session %s", ids, session_id, exc_info=True)


__all__ = [
    "CANVAS_WAIT_PREFIX",
    "CANVAS_WAIT_TOOL",
    "canvas_wait_event_key",
    "canvas_wait_filter",
    "canvas_wait_matches",
    "consume_events",
    "drop_queued_steers",
]
