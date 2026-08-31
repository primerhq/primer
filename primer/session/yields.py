"""Service-layer helper for resuming a parked session.

Extracted from the yield-respond REST endpoints so the parked_session
trigger dispatcher (Plan §5.4) can reach the same wake path without
going through HTTP. Spec §5.4.

The yielding-tools surface parks a session on
``sessions.parked_state`` (or ``chats.parked_state``); the row carries
an ``event_key`` inside ``parked_state['yielded']``. Resuming the
yield consists of publishing a payload onto that key — the event bus
listener inside each worker pool catches the publish and atomically
flips ``parked_status`` from ``"parked"`` to ``"resumable"`` so the
next claim loop picks the row up.

The router endpoints in :mod:`primer.api.routers.yields` and
:mod:`primer.api.routers.tool_approval` do this inline today. This
helper consolidates the lookup + validation + publish so:

* The parked_session dispatcher can call it directly from inside the
  trigger fire worker.
* Future yielding-tool resume callers (e.g. MCP bridge, in-process
  unit tests) don't have to re-implement the parked_state walk.

The helper is intentionally tolerant of both Session and Chat parks
— the parked_state shape is identical across both entities — but the
trigger dispatcher only targets workspace sessions today.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from primer.int.claim import ClaimKind
from primer.model.except_ import NotFoundError
from primer.model.workspace_session import SessionStatus, WorkspaceSession

if TYPE_CHECKING:
    from primer.int.claim import ClaimEngine
    from primer.int.storage import Storage


logger = logging.getLogger(__name__)


def _dispatch_key_for(event_key: str, *, session_id: str) -> str:
    """The ``resume_event_payloads`` accumulation key for ``event_key``.

    Every event_key producer emits the fixed shape
    ``"<kind>:<session_id>:<tail>"`` (``kind`` a hardcoded, colon-free
    literal like ``"tool_approval"``/``"ask_user"``; see
    primer/agent/tool_manager.py, primer/graph/_node_dispatch.py,
    primer/toolset/_system_crud.py, primer/channel/inbox.py). ``tail`` is
    a bare ``tool_call_id`` for a non-graph park, or (01a0518f)
    ``"<node_id>:<tool_call_id>"`` once a graph-checkpoint capture site
    scopes it - two concurrent fan-out siblings can legitimately share a
    raw provider tool_call_id, so accumulating multi-event replies by the
    bare tail alone silently overwrote one sibling's reply with the
    other's.

    Strips the ``"<kind>:<session_id>:"`` prefix POSITIONALLY: ``kind`` is
    recovered with ONE bounded split (safe - it's always a colon-free
    literal), then the prefix is built from that ``kind`` plus the CALLER'S
    OWN KNOWN ``session_id`` (never re-derived by counting colons in the
    string), and stripped with an exact ``startswith``/slice. This is
    immune to a session_id or a future kind ever containing a colon -
    unlike splitting the whole string positionally, which would shift
    every character after it into the wrong field. Falls back to the full
    event_key when the expected prefix isn't found (defensive; every known
    producer emits this exact shape) - degrading to today's behaviour
    rather than raising.
    """
    kind = event_key.split(":", 1)[0]
    prefix = f"{kind}:{session_id}:"
    if event_key.startswith(prefix):
        return event_key[len(prefix):]
    return event_key


async def durably_mark_session_resumable(
    session: WorkspaceSession,
    *,
    event_key: str,
    payload: dict[str, Any] | None,
    session_storage: "Storage[WorkspaceSession]",
    engine: "ClaimEngine | None",
) -> bool:
    """Guarded, durable ``parked -> resumable`` flip for one session row.

    This is the single source of truth for the park->resumable transition,
    shared by the bus listener (``primer.bus.listener.YieldEventListener``,
    which reacts to a bus NOTIFY) and the REST reply handlers (which now
    perform it durably so a listener outage cannot silently drop an operator
    reply - see arch review D-C2).

    Steps, mirroring the listener's original ``_flip_rows`` write exactly:

    * Stamp the singular ``resume_event_payload`` / ``resume_event_key`` (the
      single-event resume path + a "last fired" hint).
    * For a MULTI-event park (``parked_event_keys`` set) also accumulate
      ``resume_event_payloads[dispatch_key]`` (see :func:`_dispatch_key_for`
      - 01a0518f: the event_key's tail past the fixed ``kind:session_id:``
      prefix, node-qualified for a graph park) so a second concurrent reply
      is preserved rather than overwritten - including two fan-out siblings
      that happen to share a raw provider tool_call_id.
    * ``storage.update_unless`` the flipped row, guarded on ``status`` -
      see below.
    * Re-arm the claim lease via ``engine.mark_resumable`` (park dropped it)
      so the claim loop re-claims the row WITHOUT relying on any bus. When no
      engine is wired (e.g. the lightweight test app) the durable storage
      flip still lands; the lease re-arm is simply skipped.

    ENDED-transition race: ``parked_status`` survives the ENDED transition
    (only reopen/abandon clear it), so a session a DIFFERENT worker ended
    between the caller's own read of ``session`` and this write still
    carries ``parked_status="parked"`` on the copy passed in here. Only
    checking ``session.status`` (the caller's stale snapshot) would miss a
    row that ended IN that gap - this used to be a snapshot check for
    exactly that reason and was a real, if narrow, TOCTOU: nothing stopped
    the row from ending between the check and ``storage.update`` landing.
    The fix is a conditional write, not a better-timed read:
    ``session_storage.update_unless(..., field="status",
    forbidden=SessionStatus.ENDED.value)`` asks the BACKEND to evaluate
    "is status ENDED" against the row's CURRENT value in the same
    statement as the write (see ``Storage.update_unless``), so there is no
    gap left for the row to end in. ``flip_sessions_parked_on``'s query-level
    exclusion (below) is a separate, complementary optimization - it keeps
    an already-ended row out of the candidate set at all, which this
    function's own guard would also correctly reject if it slipped through.

    Idempotency (the listener may also process the NOTIFY): a single-event
    park only advances from ``parked``, so a second flip is a no-op; a
    multi-event park may advance from ``resumable`` and re-accumulates the
    same ``dispatch_key`` with identical data. Returns True when the row
    was advanced/accumulated, False when the guard rejected it (including
    the ENDED race above, resolved at write time rather than read time).
    """
    is_multi = bool(session.parked_event_keys)
    allowed = ("parked", "resumable") if is_multi else ("parked",)
    if session.parked_status not in allowed:
        return False
    if session.status == SessionStatus.ENDED:
        # Cheap early exit ONLY: the caller's own snapshot already says
        # ENDED, so skip the round trip. This is NOT the safety guarantee
        # (a snapshot cannot be) - update_unless below is what actually
        # closes the race for a row that ends AFTER this check runs.
        return False
    state = dict(session.parked_state or {})
    # Singular fields: the single-event resume path + a "last fired" hint.
    state["resume_event_payload"] = dict(payload or {})
    state["resume_event_key"] = event_key
    if is_multi:
        dispatch_key = _dispatch_key_for(event_key, session_id=session.id)
        payloads = dict(state.get("resume_event_payloads") or {})
        payloads[dispatch_key] = {
            "payload": dict(payload or {}),
            "event_key": event_key,
        }
        state["resume_event_payloads"] = payloads
    updated = session.model_copy(update={
        "parked_status": "resumable",
        "parked_state": state,
    })
    landed = await session_storage.update_unless(
        updated, field="status", forbidden=SessionStatus.ENDED.value,
    )
    if landed is None:
        # The row's CURRENT status was ENDED at write time - rejected
        # atomically, not from the (possibly stale) snapshot above.
        return False
    # Re-arm the engine lease (park dropped it). mark_resumable upserts a
    # fresh claimable lease when none exists.
    if engine is not None:
        await engine.mark_resumable(ClaimKind.SESSION, session.id)
    return True


async def durably_wake_session(
    session: WorkspaceSession,
    *,
    event_key: str,
    payload: dict[str, Any] | None,
    session_storage: "Storage[WorkspaceSession]",
    engine: "ClaimEngine | None",
) -> bool:
    """Durable flip for the REST reply handlers, repairing a missing lease.

    :func:`durably_mark_session_resumable` writes twice and the two writes
    CANNOT share a transaction (``mark_resumable`` acquires its own
    connection). So a crash between them leaves the row ``resumable`` with
    NO lease row - and ``claim_due`` JOINs the leases table, which makes the
    session permanently unclaimable. The reply handlers must not report the
    reply accepted in that state.

    This wrapper ACTS on the helper's return value instead of discarding it:
    a False return on a row whose ``parked_status`` is already ``resumable``
    is exactly the fingerprint of that half-applied flip (the guard only
    admits ``parked`` for a single-event park), so re-drive
    ``mark_resumable`` - an idempotent upsert - to re-create the lease the
    first attempt lost. When the lease is already healthy the upsert is a
    harmless no-op, which is the common case for an ordinary double-reply.

    A raising ``storage.update_unless`` still propagates untouched: the
    caller must NOT report a reply accepted when the durable stamp never
    landed.

    Returns the underlying helper's bool (True when this call advanced the
    row, False when the guard rejected it).
    """
    did = await durably_mark_session_resumable(
        session,
        event_key=event_key,
        payload=payload,
        session_storage=session_storage,
        engine=engine,
    )
    if did or engine is None:
        return did
    if session.parked_status != "resumable":
        # Guard rejected for some other reason (not a half-applied flip);
        # there is no lease to repair.
        return did
    logger.info(
        "Repairing claim lease for session %s: the row is already "
        "'resumable' but the durable flip may not have re-armed its lease",
        session.id,
    )
    await engine.mark_resumable(ClaimKind.SESSION, session.id)
    return did


@dataclass
class RespondToYieldDeps:
    """Collaborators :func:`respond_to_yield` needs.

    Kept tiny on purpose — the helper does one storage lookup and one
    bus publish; everything else lives inside the worker pool's bus
    listener and the resume-classifier in
    :mod:`primer.worker.yield_runtime`.
    """

    storage_provider: Any
    event_bus: Any


def _tool_call_id_for(blob: dict[str, Any]) -> str | None:
    """Resolve tool_call_id from a parked_state blob.

    Mirrors :func:`primer.api.routers.yields._tool_call_id_for`. Worker
    writes it at the top level; older parks may have only had it inside
    ``yielded.resume_metadata``. Falling back keeps the lookup robust
    across upgrades.
    """
    tcid = blob.get("tool_call_id")
    if tcid:
        return tcid
    yielded = blob.get("yielded") or {}
    metadata = yielded.get("resume_metadata") or {}
    return metadata.get("tool_call_id")


# Tokens that read as an affirmative approval. Matched case-folded
# against the reply's whitespace-split tokens.
_AFFIRMATIVE = {"yes", "y", "approve", "approved", "ok", "okay", "sure", "go"}
# Tokens that read as a refusal. A negative anywhere in the reply vetoes
# a co-occurring affirmative ("no yes" -> rejected) so the parse fails
# closed against ambiguous intent, which is the only safe direction for
# something that decides whether a tool runs.
_NEGATIVE = {
    "no", "n", "nope", "nah", "deny", "denied", "reject", "rejected",
    "cancel", "stop", "dont", "don't", "do not",
}


def classify_approval_text(text: str) -> bool | None:
    """Read a free-text reply to an approval gate.

    Returns True to approve, False to reject, and None when the reply
    is not a decision at all, so the caller can keep asking rather than
    guess.

    Ported verbatim from the chat surface, including its tokenisation:
    replies are lowercased and split on whitespace, with no punctuation
    stripping. "yes." therefore does not approve, and the multi-word
    "do not" entry above can never match. Both are worth fixing, but not
    silently inside a port, because either change alters which replies
    approve a tool call.
    """
    tokens = (text or "").strip().lower().split()
    if not tokens:
        return None
    if any(t in _NEGATIVE for t in tokens):
        return False
    if any(t in _AFFIRMATIVE for t in tokens):
        return True
    return None


async def respond_to_yield(
    *,
    session_id: str,
    tool_call_id: str,
    result: Any,
    deps: RespondToYieldDeps,
) -> None:
    """Publish *result* onto the parked session's resume ``event_key``.

    Steps:

    1. Look up the :class:`WorkspaceSession` row.
    2. Validate the row is parked (or already resumable) and that its
       in-flight ``tool_call_id`` matches.
    3. Pull ``event_key`` out of the parked_state blob.
    4. Publish ``result`` onto that key via the event bus. The bus
       listener inside the worker pool flips the row to ``resumable``.

    Raises
    ------
    NotFoundError
        When the session doesn't exist, isn't parked, or is parked on
        a different ``tool_call_id``.

    Notes
    -----
    The helper does NOT write to ``parked_state`` itself — the worker
    pool's bus listener owns that flip via the scheduler's atomic
    ``mark_resumable``. Calling this helper twice for the same yield is
    a no-op once the first publish has flipped the row to ``resumable``
    (the second publish goes onto the bus too, but ``mark_resumable``
    is idempotent so duplicate flips are harmless).
    """
    storage = deps.storage_provider.get_storage(WorkspaceSession)
    session = await storage.get(session_id)
    if session is None:
        raise NotFoundError(f"Session {session_id!r} does not exist")

    if session.parked_status not in ("parked", "resumable"):
        raise NotFoundError(
            f"Session {session_id!r} has no in-flight yield to resume"
        )
    blob: dict[str, Any] = session.parked_state or {}
    expected = _tool_call_id_for(blob)
    if expected != tool_call_id:
        raise NotFoundError(
            f"No in-flight yield with tool_call_id {tool_call_id!r} "
            f"on session {session_id!r}"
        )

    yielded: dict[str, Any] = blob.get("yielded") or {}
    event_key: str | None = yielded.get("event_key")
    if not event_key:
        # Defensive — every park written by the current runtime carries
        # an event_key. A missing one means a corrupted park; the
        # caller's only sensible recourse is to surface 404 like the
        # REST endpoint does.
        raise NotFoundError(
            f"Session {session_id!r} park is missing event_key"
        )

    payload: dict[str, Any]
    if isinstance(result, dict):
        payload = result
    else:
        payload = {"response": result}
    await deps.event_bus.publish(event_key, payload)


__all__ = [
    "RespondToYieldDeps",
    "durably_mark_session_resumable",
    "durably_wake_session",
    "respond_to_yield",
]


async def flip_sessions_parked_on(
    event_key: str,
    payload,
    *,
    session_storage,
    engine,
) -> int:
    """Find every session parked on ``event_key`` and durably flip it.

    The single shared core behind BOTH wake deliveries: the volatile
    bus's YieldEventListener (transport-fast) and the event-log
    dispatcher's flip sink (durable replay). Guarded flips make the
    two racing each other a harmless no-op.

    Single-event parks match on the singular ``parked_event_key``; a
    membership fallback covers multi-event parks (graph supersteps),
    gated to human-reply keys so the common path stays one keyed
    query. Returns the number of rows advanced.
    """
    from primer.model.storage import FieldRef, OffsetPage, Op, Predicate, Value

    def _excluding_ended(pred: Predicate) -> Predicate:
        """AND *pred* with status != ENDED.

        parked_status survives the ENDED transition (only reopen/abandon
        clear it), so a session a DIFFERENT worker ended before this
        find() runs still matches parked_status="parked" here. This is a
        candidate-set optimization, not the safety guarantee: it just
        keeps an already-ended row out of the loop below entirely, so it
        never reaches durably_mark_session_resumable at all in the common
        case. That function's own write is independently guarded (its
        storage.update_unless call, atomic against the row's CURRENT
        status) - so a row that slips past this filter, or ends in the
        narrower gap between this find() and that write, is still
        rejected there rather than getting a lease armed on it and
        hitting workspace_executor's "cannot invoke ENDED session" guard.
        """
        return Predicate(
            left=pred,
            op=Op.AND,
            right=Predicate(
                left=FieldRef(name="status"),
                op=Op.NE,
                right=Value(value=SessionStatus.ENDED.value),
            ),
        )

    predicate = _excluding_ended(Predicate(
        left=Predicate(
            left=FieldRef(name="parked_status"),
            op=Op.EQ,
            right=Value(value="parked"),
        ),
        op=Op.AND,
        right=Predicate(
            left=FieldRef(name="parked_event_key"),
            op=Op.EQ,
            right=Value(value=event_key),
        ),
    ))
    page = await session_storage.find(predicate, OffsetPage(length=200))
    flipped = 0
    for sess in page.items:
        if await durably_mark_session_resumable(
            sess, event_key=event_key, payload=payload,
            session_storage=session_storage, engine=engine,
        ):
            flipped += 1

    if flipped == 0 and event_key.startswith(("ask_user:", "tool_approval:")):
        member_pred = _excluding_ended(Predicate(
            left=Predicate(
                left=FieldRef(name="parked_status"),
                op=Op.IN,
                right=Value(value=["parked", "resumable"]),
            ),
            op=Op.AND,
            right=Predicate(
                left=FieldRef(name="parked_event_keys"),
                op=Op.CONTAINS,
                right=Value(value=event_key),
            ),
        ))
        page2 = await session_storage.find(member_pred, OffsetPage(length=200))
        for sess in page2.items:
            if await durably_mark_session_resumable(
                sess, event_key=event_key, payload=payload,
                session_storage=session_storage, engine=engine,
            ):
                flipped += 1
    return flipped

