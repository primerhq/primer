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
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import primer.observability.metrics as _metrics
from primer.int.claim import ClaimKind
from primer.model.except_ import NotFoundError
from primer.model.tool_call_task import MalformedScopedIdError, parse_scoped_task_id
from primer.model.common import dump_for_storage
from primer.model.yield_ import with_wake_entry, with_wake_park
from primer.model.workspace_session import NON_ENDED_STATUSES, SessionStatus, WorkspaceSession
from primer.storage import raw_generation
from primer.storage.cas import patch_if_checked

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

    A ``tool_wait`` key (the reserved kind of :func:`tool_wait_event_key`)
    is returned WHOLE. Its tail ``<turn_seg>:<node>`` could equal a human
    gate's ``<node>:<tool_call_id>`` once a node id contains ``:`` (a gate
    on node ``5`` with tool_call_id ``a:b`` and the batch of node ``a:b``
    in turn 5 are both ``5:a:b``), and the second write would overwrite the
    human reply. Nothing reads the dict key (the resume iterates the
    entries and reads each one's own ``event_key``), so only uniqueness
    matters, and a tool_wait park exists only with the claims flag on, so
    no other kind's key changes.

    Every other kind strips the ``"<kind>:<session_id>:"`` prefix POSITIONALLY: ``kind`` is
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
    if kind == "tool_wait":
        return event_key
    prefix = f"{kind}:{session_id}:"
    if event_key.startswith(prefix):
        return event_key[len(prefix):]
    return event_key


_LEAF_KEY_ESCAPED = frozenset('%"\\')


def leaf_key_for(dispatch_key: str) -> str:
    """The ``resume_event_payloads`` dict key under which the leaf for ``dispatch_key`` is stored.

    The dispatch key (see :func:`_dispatch_key_for`) ends in a graph NODE id (constrained only by
    ``min_length=1``) or, on a non-graph park, a raw provider ``tool_call_id``, so it can hold any character.
    ``patch_if`` refuses a ``set_paths`` element that holds a double quote, a backslash or a control character
    (``PatchSpecError``: the SQLite JSON path is built as ``$."<element>"`` and a backslash-escaped quote is a bad
    path on SQLite 3.45), so a write that used the raw key would make a park with such an id unwakeable, which
    the whole-document write it replaces never was.

    The key is percent-encoded for exactly ``%``, ``"``, ``\\`` and every character below U+0020 (``%`` is
    escaped too, so the mapping is injective: ``a"b`` becomes ``a%22b`` and the literal ``a%22b`` becomes
    ``a%2522b``). A key with none of those characters is returned UNCHANGED, so the keys every producer writes
    today keep their spelling. Only uniqueness of the dict key matters: graph resume and the executors iterate
    ``.values()`` and read each entry's own ``event_key``, which stays the original string.

    A key that holds a surrogate code point is still refused by ``patch_if`` (nothing can store it); that is not
    this function's to repair.
    """
    return "".join(
        f"%{ord(ch):02X}" if ch in _LEAF_KEY_ESCAPED or ord(ch) < 0x20 else ch for ch in dispatch_key
    )


def tool_wait_event_key(session_id: str, *, scoped_task_id: str) -> str:
    """The wake key for a tool_wait batch's park (01a0518b review):
    ``tool_wait:<session_id>:<turn_seg>:<node>``.

    A PURE function of the batch's task id, with no stored copy and no
    turn argument - deliberately NOT a stored field on the row (an earlier
    draft stamped it at creation time; review flagged that as the same
    denormalized-projection shape that caused the ``pending_dispatch``
    disease elsewhere - two sites carrying what must be one truth). Every
    site that needs it (dispatch.py's park arms, the materializer, the
    graph re-park builder and ``ToolCallClaimAdapter.on_release``'s
    last-sibling wake) calls THIS function with a task id of the batch, so
    there is exactly one place the shape can drift from.

    ``scoped_task_id``: any ``ToolCallTask.id`` belonging to the batch,
    session-qualified or bare (all of a batch's ids share the same node and
    turn segment). It is parsed by
    :func:`primer.model.tool_call_task.parse_scoped_task_id` (the one
    parser), which raises ``MalformedScopedIdError`` for an id it cannot
    parse; each caller decides what a malformed id means for it.

    * The node is the FULL node id: the id is split from the right, so a
      graph node id containing ``:`` keeps it (``a:b`` and ``a:c`` are two
      keys, not one). ``"x"`` for the chat/workspace surface's own
      ``node_id=None`` convention. Taken from the id rather than a separate
      ``node_id`` parameter, so BOTH surfaces compute this key from exactly
      the same source data.
    * The turn segment is the one the id was minted with, as written.
      Every id written today carries the plain turn number
      (``scoped_tool_call_id`` formats an int turn number only, so the
      segment is ``<turn_no>``). The parser also ACCEPTS
      ``<turn_no>.<epoch>`` (a retired-turn epoch, epoch > 0), which
      nothing writes yet, so that a future writer's ids keep working and
      their keys stay distinct from the retired turn's. It is the turn
      that MATERIALIZED the batch, which a batch's ids carry through every
      round trip, so the key does not move when the session's own turn
      counter does (a key computed from ``session.turn_no`` diverged from
      one computed from ``task.turn_no`` after a park-preserving bump).

    The graph surface can have SEVERAL concurrent fan-out siblings each
    raise their OWN ``ToolWaitPark`` in the SAME superstep (same
    ``turn_no``) - mirroring how a human approval gate is already
    addressed per-``tool_call_id`` (see ``_PendingToolCall``/
    ``_PendingAgentYield``), each node's batch needs its own
    independently-derivable key so "one gated call's siblings don't
    block another node's siblings" holds for tool_wait batches too, not
    just human gates. The chat/workspace surface's own scoped ids all
    share the SAME ``"x"`` node segment (only one batch can ever be
    pending per turn there - a fresh turn always mints a fresh
    ``_CoalesceState``), so this collapses to one key per turn exactly
    as before.

    Shaped like every other event_key producer
    (``"<kind>:<session_id>:<tail>"``); the ``tool_wait`` kind is reserved
    to this function, and :func:`_dispatch_key_for` returns its keys whole.

    Distinct from ``ToolWaitPark.event_key`` (observability-only,
    ``f"tool_wait:{outstanding_task_ids[0]}"`` - never looked up by
    anything, see that class's own docstring) - this is the FUNCTIONAL
    key the wake mechanism actually keys on.
    """
    parsed = parse_scoped_task_id(scoped_task_id, session_id)
    return f"tool_wait:{session_id}:{parsed.turn_seg}:{parsed.node}"


def tool_wait_event_key_or_none(session_id: str, *, scoped_task_id: str, site: str) -> str | None:
    """:func:`tool_wait_event_key`, or ``None`` for a malformed id: logged at ERROR and counted under ``site``.

    Never a guessed key. What a missing key means is the caller's call: the
    adapter (``site="adapter"``) wakes nothing, because it runs inside the
    release transaction and a raise there would roll back the task's
    result; the materializer and the graph re-park builder leave that
    batch's key out of the park; and a park arm left with no key at all
    ends the turn failed rather than write a park nothing can wake (with no
    ``parked_event_key`` it would have no timeout backstop either). Only an
    id this code minted itself can get here, so any count is a bug.
    """
    try:
        return tool_wait_event_key(session_id, scoped_task_id=scoped_task_id)
    except MalformedScopedIdError as exc:
        _metrics.tool_wait_malformed_scoped_id_total.labels(site).inc()
        logger.error(
            "session %s: no tool_wait wake key at %s for task id %r: %s",
            session_id, site, scoped_task_id, exc,
        )
        return None


def _wake_names_another_gate(session: WorkspaceSession, *, event_key: str, payload: dict[str, Any] | None) -> bool:
    """Whether ``payload`` is a decision for a gate other than the one the session now has pending on ``event_key`` (C-033 round 2, PR 4).

    A decision's wake is delivered by event key alone and at least once, so one redelivered after the session resumed and PARKED AGAIN under the same
    provider id carries the same event key as the new gate. The wake names the gate it decided (:data:`~primer.model.yield_.WAKE_GATE_ID_KEY`); when
    it does, and the pending entry that waits on ``event_key`` carries an id, the two must be equal, else the flip is refused: nothing is written, one
    WARNING and ``session_wake_gate_refused_total``. A wake with no id (written before this release, or a decision on a yield that is not a human gate)
    and a pending entry with none (a park from before gates had ids) are judged by the event key alone, as before.
    """
    from primer.model.yield_ import WAKE_GATE_ID_KEY, gate_id_of
    from primer.session.pending_gates import enumerate_pending_gates

    named = (payload or {}).get(WAKE_GATE_ID_KEY)
    if not named:
        return False
    pending = [
        gate_id_of(entry.get("resume_metadata"))
        for entry in enumerate_pending_gates(session.parked_state or {})
        if entry.get("event_key") == event_key
    ]
    pending = [gate_id for gate_id in pending if gate_id]
    if not pending or named in pending:
        return False
    _metrics.session_wake_gate_refused_total.inc()
    logger.warning(
        "session %s: refused a wake on %r that decided another gate than the pending one (it was redelivered after the session re-parked under the same key)",
        session.id, event_key,
    )
    return True


def _wake_names_another_entry(session: WorkspaceSession, *, event_key: str, payload: dict[str, Any] | None) -> bool:
    """Whether a MACHINE wake answers a pending entry other than the one that waits on ``event_key`` now (security ticket 01a1223f, #702 review N4).

    The park stamp fences a single park; a graph park re-parks with a fresh ``parked_at`` whenever a sibling is resolved, so it is exempt. What survives a
    re-park is the identity of the ENTRY: the subscription a trigger fire or a ``wait_for_event`` delivery answers (``resume_metadata.subscription_id``), the call
    row an external result answers (``external_call_row_id``). A producer that knows it names it (:data:`~primer.model.yield_.WAKE_ENTRY_KEY`); when it does, and
    an entry that waits on ``event_key`` carries an identity, the wake must name one of them, else the flip is refused: nothing is written, one WARNING and
    ``session_wake_stale_refused_total``. A wake that names none (an older producer) and a pending entry with none (a park from before) are judged as before.
    A single park and a graph park are judged alike. Two graph siblings that share a key are not told apart: a wake that names either is admitted, and
    the resume takes every entry on the key (ticket 01a122cc-e699).
    """
    from primer.model.yield_ import WAKE_ENTRY_KEY, entry_id_of
    from primer.session.pending_gates import enumerate_pending_gates

    named = (payload or {}).get(WAKE_ENTRY_KEY)
    if not named:
        return False
    pending = [
        entry_id_of(entry.get("resume_metadata"))
        for entry in enumerate_pending_gates(session.parked_state or {})
        if entry.get("event_key") == event_key
    ]
    pending = [entry_id for entry_id in pending if entry_id]
    if not pending or named in pending:
        return False
    _metrics.session_wake_stale_refused_total.inc()
    logger.warning(
        "session %s: refused a wake on %r that answers another entry than the pending one (it was redelivered after the session re-parked under the same key, "
        "or it was published for another park that shares the key)",
        session.id, event_key,
    )
    return True


_TIMEOUT_CLOCK_SKEW = timedelta(seconds=5)
"""How far ahead of the flipping node's clock a park's deadline may be for a timeout marker or timer fire to still apply (the publisher selected the row on ITS clock).

A sleep of this length or less can therefore be ended early by a stale fire that arrives inside the window (acceptable: the scheduler's own granularity is a couple
of seconds, and ``elapsed_seconds`` in the sleep's result stays true)."""


def _wake_is_for_an_earlier_park(session: WorkspaceSession, *, event_key: str, payload: dict[str, Any] | None) -> bool:
    """Whether a MACHINE wake belongs to an earlier park than the one the session now has pending on ``event_key`` (security ticket 01a1208d).

    A machine wake is delivered by event key alone and at least once, so one redelivered after the session resumed and PARKED AGAIN under the same provider
    id carries the key of the new park. Two checks, each refusing with nothing written, one WARNING and ``session_wake_stale_refused_total``:

    * a TIMEOUT marker, or a TIMER fire (the empty payload the ``TimerScheduler`` publishes on a ``timer:`` key), applies only to a park whose deadline has passed
      (``parked_until`` no more than a few seconds ahead of this node's clock); the redelivered marker of an expired gate otherwise timed out the later gate, and one
      session's timer woke the sleep of another that waited on the same key (ticket 01a12151-b225), whose own deadlines were still ahead. A park with no deadline is
      judged as before;
    * a wake that names the park its producer read (:data:`~primer.model.yield_.WAKE_PARK_KEY`: a trigger fire, an external result, the cancel of a non-gate
      yield, the steer route's cancel of external calls, a ``wait_for_event`` delivery) must name the park now pending. A graph park is not judged by the stamp:
      resolving one sibling re-parks the graph on the others with a fresh ``parked_at``.

    A cancel is judged by the stamp it carries and never by the deadline (it is a decision, not a clock). Everything else is judged by the event key alone, as
    before: a human decision (fenced by its gate id instead, :func:`_wake_names_another_gate`), a file-watcher event or an MCP bridge result (neither carries a
    stamp), and a wake written before this release.
    """
    from primer.model.yield_ import WAKE_PARK_KEY
    from primer.worker.yield_runtime import is_timeout_payload

    is_timer_fire = event_key.startswith("timer:") and not payload
    if (is_timeout_payload(payload) or is_timer_fire) and session.parked_until is not None:
        if session.parked_until > datetime.now(timezone.utc) + _TIMEOUT_CLOCK_SKEW:
            _metrics.session_wake_stale_refused_total.inc()
            logger.warning(
                "session %s: refused a %s on %r: the pending park's deadline (%s) is still ahead (it was redelivered after the session re-parked under the same "
                "key, it was published for another park that shares the key, or this node's clock runs behind the publisher's: clock skew, which resolves itself "
                "because the publisher republishes while the park stays due)",
                session.id, "timer fire" if is_timer_fire else "timeout marker", event_key, session.parked_until.isoformat(),
            )
            return True
    stamp = (payload or {}).get(WAKE_PARK_KEY)
    if stamp and session.parked_at is not None and not (session.parked_state or {}).get("graph_checkpoint"):
        try:
            named = datetime.fromisoformat(str(stamp))
        except ValueError:
            return False
        if named != session.parked_at:
            _metrics.session_wake_stale_refused_total.inc()
            logger.warning(
                "session %s: refused a wake on %r that names an earlier park than the pending one (it was redelivered after the session re-parked under the same "
                "key, or it was published for another park that shares the key)",
                session.id, event_key,
            )
            return True
    return False


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
      prefix, node-qualified for a graph park; a tool_wait key whole) so a second reply
      is preserved rather than overwritten - including two fan-out siblings
      that happen to share a raw provider tool_call_id - PROVIDED it read the
      row the first one wrote: the leaf is merged into the snapshot's
      ``parked_state``, which the write replaces whole. A caller that wakes
      several keys of one park re-reads the row before each further wake
      (``apply_tool_results``, the steer route's cancel of external calls);
      two replies that race from one snapshot can still drop a leaf (a
      ``set_paths`` leaf per dispatch key would close it, ticket 01a122cc-effa).
    * ONE ``patch_if`` of the two fields the flip owns (``parked_status``,
      ``parked_state``), guarded on the park it read (``parked_at``), a
      ``parked_status`` it may advance from and a status that is not ENDED -
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
    The fix is a conditional write, not a better-timed read: the
    ``patch_if`` guard admits every status but ENDED, and the BACKEND
    evaluates it against the row's CURRENT value in the same statement as
    the write (see ``Storage.patch_if``), so there is no gap left for the
    row to end in. ``flip_sessions_parked_on``'s query-level
    exclusion (below) is a separate, complementary optimization - it keeps
    an already-ended row out of the candidate set at all, which this
    function's own guard would also correctly reject if it slipped through.

    Idempotency (the listener may also process the NOTIFY): a single-event
    park only advances from ``parked``, so a second flip is a no-op; a
    multi-event park may advance from ``resumable`` and re-accumulates the
    same ``dispatch_key`` with identical data. Returns True when the row
    was advanced/accumulated, False when the guard rejected it (including
    the ENDED race above, resolved at write time rather than read time).

    ``ToolCallClaimAdapter.on_release`` (01a0518b, the mixed-park wake
    seam) is a NEW caller, but never calls this directly from INSIDE its
    own claim-engine transaction - see
    :class:`primer.int.claim.PostReleaseWake`'s own docstring for why an
    adapter calling this function mid-transaction would let a worker
    observe a stale (pre-commit) entity row. This function is only ever
    invoked standalone (no surrounding transaction of its own), by
    design, from every caller including that one.
    """
    is_multi = bool(session.parked_event_keys)
    allowed = ("parked", "resumable") if is_multi else ("parked",)
    if session.parked_status not in allowed:
        return False
    if _wake_names_another_gate(session, event_key=event_key, payload=payload):
        return False
    if _wake_is_for_an_earlier_park(session, event_key=event_key, payload=payload):
        return False
    if _wake_names_another_entry(session, event_key=event_key, payload=payload):
        return False
    if session.status == SessionStatus.ENDED:
        # Cheap early exit ONLY: the caller's own snapshot already says
        # ENDED, so skip the round trip. This is NOT the safety guarantee
        # (a snapshot cannot be) - the guarded patch_if below is what actually
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
    # ONE guarded patch of the two fields the flip owns, evaluated by the backend against the CURRENT row (ticket 01a1223f, #702 review N9). The fences above
    # judged ``session``, the row ``find()`` returned; a whole-document write guarded on ``status`` alone let a wake that read the old park land on a park
    # the session had meanwhile entered, and rewrote every other field from the stale snapshot. The guard is the park that was read (``parked_at``), a
    # ``parked_status`` the flip may advance from (``parked``; ``parked`` or ``resumable`` for a multi-event park) and a status that is not ENDED (the race
    # described above).
    # The park is named by its stored spelling. The storage layer writes ``parked_at`` canonically (pydantic's ``Z`` form, ``raw_generation``); a park
    # written outside it (raw SQL, an older build) can hold the ``isoformat()`` spelling (``+00:00``) of the SAME instant, which names the same park, so
    # both are accepted: a guard on the canonical one alone refused every wake of such a park for ever (#707 review round 2, B1).
    parked_at_spellings = [raw_generation(session, "parked_at")]
    if session.parked_at is not None and session.parked_at.isoformat() not in parked_at_spellings:
        parked_at_spellings.append(session.parked_at.isoformat())
    dumped = dump_for_storage(updated)
    landed = await patch_if_checked(
        session_storage,
        session.id,
        {"parked_status": dumped["parked_status"], "parked_state": dumped["parked_state"]},
        where={"parked_at": parked_at_spellings, "parked_status": list(allowed), "status": NON_ENDED_STATUSES()},
    )
    if landed is None:
        # The row is no longer the one this wake read: it ended, resumed, or parked again. Rejected atomically at write time, not from the (possibly
        # stale) snapshot above.
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

    A raising ``patch_if`` (a missing row's ``NotFoundError`` too) still propagates untouched: the
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
    entry_id: str | None = None,
) -> None:
    """Publish *result* onto the parked session's resume ``event_key``.

    ``entry_id`` is the identity of the pending entry the producer answers (a trigger fire passes its subscription's id): the wake names it, so the flip refuses a
    copy delivered after the session re-parked, graph parks included (:func:`_wake_names_another_entry`).

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
    # The wake names the park this producer read: it is delivered by key alone, and one redelivered after the session re-parked under the same key must
    # not decide the new park (security ticket 01a1208d).
    await deps.event_bus.publish(event_key, with_wake_entry(with_wake_park(payload, session.parked_at), entry_id))


__all__ = [
    "RespondToYieldDeps",
    "durably_mark_session_resumable",
    "durably_wake_session",
    "leaf_key_for",
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
        one patch_if, atomic against the row's CURRENT
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

