from __future__ import annotations
from abc import ABC, abstractmethod
from collections.abc import Awaitable, AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any


class ClaimKind(StrEnum):
    SESSION = "session"
    HARNESS = "harness"
    TRIGGER = "trigger"
    TOOL_CALL = "tool_call"


@dataclass(frozen=True)
class Lease:
    kind: ClaimKind
    entity_id: str
    claimed_by: str
    claimed_at: datetime
    expires_at: datetime
    attempt_count: int
    last_error: str | None


@dataclass(frozen=True)
class ParkRequest:
    """Request to park an entity instead of completing/failing its turn.

    Set on :class:`ReleaseOutcome` when a session turn hits a yielding
    tool. The claim adapter's ``on_release`` writes these into the
    entity row's park columns (parked_status='parked') and the engine
    drops the lease, so the parked entity is not re-claimed until the
    resume event re-arms it.
    """

    parked_state: dict[str, Any]
    parked_event_key: str
    parked_until: datetime | None
    parked_at: datetime
    # Multi-event park: the full set of keys the session waits on (any one
    # firing wakes it). None for the common single-event park.
    parked_event_keys: list[str] | None = None


#: Claim priorities (lower is claimed first). Three named values instead of scattered literals: a
#: priority is a scheduling contract, and the 7a TOOL_CALL arm sites must say which one they mean.
CLAIM_PRIORITY_OPERATOR = 10  #: an operator asked for it (harness operations, trigger seeding)
CLAIM_PRIORITY_RESUME = 50    #: resume-from-park (``mark_resumable``'s default), and every TOOL_CALL arm
CLAIM_PRIORITY_FRESH = 100    #: fresh work (``upsert``'s default)


@dataclass(frozen=True)
class ReleaseOutcome:
    success: bool
    requeue_after: timedelta | None = None
    last_error: str | None = None
    drop_lease: bool = False
    park: ParkRequest | None = None
    # When True, on_release leaves the entity's park columns (parked_status,
    # parked_state, parked_event_keys, parked_at) untouched; it still bumps
    # turn_no on success (the session adapter's preserve-park branch). Used
    # by the pause-while-parked path: the operator paused a resumable session,
    # so the lease drops but the park is retained for a later /resume to
    # replay. Mutually exclusive with ``park`` (which re-parks instead).
    preserve_park: bool = False
    # When True, the ENGINE gives the lease back (drop it with ``drop_lease``, or requeue it) and
    # does NOT call the kind's ``adapter.on_release`` at all: the entity row is not read or written.
    # For a lease that never reached its handler (a claim that landed after shutdown began) or one the
    # handler is handing back without having touched the entity. Any ``on_release`` is wrong there:
    # the session adapter's non-park branch would clear a resumable session's park and bump
    # ``turn_no`` for a turn that never ran. Honoured by every engine, so no adapter needs to know.
    # Lease-neutral on a requeue: ``attempt_count`` and ``last_error`` are left as they were (a hand-back
    # is not a run, successful or not), and so is ``next_attempt_at`` unless ``requeue_after`` is given.
    # Cannot be combined with ``park`` or ``preserve_park`` (those are entity writes).
    entity_noop: bool = False
    # Fields the adapter writes onto the entity in the SAME fenced release transaction,
    # as one field-scoped ``Storage.patch_if`` (never a whole-document update). Which keys
    # an adapter accepts is its own contract; ``ToolCallClaimAdapter`` validates them PER
    # BRANCH (terminal: ``result_state``, ``attempts``, ``last_error``; retry: ``attempts``,
    # ``last_error``; gated: none). Any other key, or a value the model refuses, FAILS THE TASK
    # terminally (through the same fence) rather than raising inside the release transaction, and
    # a handler can never overwrite ``state``, ``claim_token`` or ``finished_at``.
    entity_update: Mapping[str, Any] | None = None
    # The per-claim token the releaser holds. ``ToolCallClaimAdapter`` writes the entity only
    # while the row still carries it, and refuses the write when it is ``None``: a ``None``
    # token never matches, so a token-less release cannot move a row someone else now owns.
    claim_token: str | None = None

    def __post_init__(self) -> None:
        if self.entity_noop and (self.park is not None or self.preserve_park):
            raise ValueError("ReleaseOutcome(entity_noop=True) cannot carry a park or preserve_park")
        if self.entity_noop and (self.entity_update or self.claim_token is not None):
            # A lease-only release never reaches the adapter, so a fenced entity write (an entity_update, or the
            # claim_token that fences one) would be silently dropped: that is a caller bug, refused here.
            raise ValueError("ReleaseOutcome(entity_noop=True) cannot carry an entity_update or a claim_token")


@dataclass(frozen=True)
class PostReleaseWake:
    """Optional signal ``ClaimAdapter.on_release`` returns to ask the
    ENGINE to wake something else AFTER this release's own transaction
    commits (01a0518b review - the mixed-park wake seam's hazard fix).

    The engine owns transaction boundaries, adapters don't: an adapter
    has no "my transaction just committed" hook of its own to call
    worker-layer code from. ``ToolCallClaimAdapter.on_release`` calling
    ``primer.session.yields.durably_mark_session_resumable`` directly
    (an earlier draft) would run that write on a SEPARATE connection
    that could commit BEFORE the release's own surrounding
    ``conn.transaction()`` does - a worker claiming the newly-resumable
    session could then observe THIS release's own entity-state write in
    a stale, pre-commit state. Returning this signal instead defers the
    actual wake call until the engine's own transaction has fully
    committed (see ``ClaimEngine.bind_post_release_hook``).

    ``session_id`` / ``event_key`` / ``payload`` mirror
    ``durably_mark_session_resumable``'s own parameters, minus the
    ``session`` row itself - the bound hook re-reads it fresh, since a
    row snapshotted INSIDE the now-committed transaction could itself
    already be stale by the time the hook actually runs.

    Every adapter OTHER than ``ToolCallClaimAdapter`` returns ``None``
    (the default for a function with no ``return`` statement) - this is
    purely additive, no other adapter's ``on_release`` contract changes.

    Accepted crash window: if the process dies AFTER this release's
    transaction commits but BEFORE the bound hook actually runs, the
    task goes terminal but the session never gets woken - it degrades to
    the EXISTING park-timeout backstop (consistent with the system's
    at-least-once philosophy elsewhere), not a permanently stuck park.
    Recovery-boot reconciliation (re-arming any tool_wait park whose
    siblings are ALL already terminal) is a deliberate follow-up, not
    built here.

    Why not just thread ``conn`` into ``durably_mark_session_resumable``
    instead of this signal split (the option considered and rejected
    before this shape landed): that only fixes HALF the hazard. Even
    with the row-flip conn-scoped into the release's own transaction,
    ``ClaimEngine.mark_resumable`` (the lease re-arm) has NO ``conn``
    parameter on Postgres at all - it always acquires a fresh connection
    and each statement auto-commits outside any transaction. That means
    the lease could still be armed and immediately claimable WHILE the
    release's own transaction (containing the row-flip) is still open.
    Traced concretely: a worker claiming that lease reads the session
    row fresh: if the flip hasn't committed yet, ``parked_status`` still
    reads ``"parked"``, ``WorkerPool``'s claim-dispatch (pool.py
    ~654-680) falls through its ``else`` branch and runs an ORDINARY,
    FRESH turn via ``run_one_session_turn`` - for a graph session this
    means restarting the WHOLE GRAPH FROM ITS BEGIN NODE while the real
    park-write is still mid-flight. Not a benign no-op-and-repoll; an
    active correctness risk, worse than a plain lost wake. Extending
    ``conn`` all the way into ``mark_resumable`` too (making the ENTIRE
    wake fully transactional) was also considered and rejected: it buys
    nothing this post-commit-signal design doesn't already have (zero
    exposure to the hazard, by construction - nothing runs until after
    the release's transaction has already committed), at real added
    plumbing cost (a new engine-level API on the Postgres connection
    surface, for both engines).
    """

    session_id: str
    event_key: str
    payload: dict[str, Any]


class ClaimAdapter(ABC):
    kind: ClaimKind
    entity_table: str

    @abstractmethod
    def eligibility_sql(self) -> str: ...

    @abstractmethod
    async def on_release(
        self, conn, entity_id: str, *, outcome: ReleaseOutcome,
    ) -> "PostReleaseWake | None": ...

    def entity_indexes(self, qualified_table: str) -> list[str]:
        """Return ``CREATE INDEX`` statements backing this adapter's queries.

        Default: none. Adapters whose eligibility SQL (or the bus
        listener's lookups) filter on JSONB fields override this to
        declare backing indexes; the Postgres engine creates them
        idempotently alongside the entity table. ``qualified_table`` is
        the schema-qualified table name to target. Each statement MUST be
        ``CREATE INDEX IF NOT EXISTS`` so creation is safe to repeat and
        race with peer processes.
        """
        return []

    def dead_lease_sql(self) -> str | None:
        """SQL predicate marking a lease of this kind as garbage, or ``None`` (the default).

        A predicate over an EXISTING entity row aliased ``e`` (like :meth:`eligibility_sql`), true when the
        entity is finished and its lease can never usefully be claimed again. The engine adds the
        missing-entity case itself (no row at all), so the predicate never has to name it. ``None`` opts the
        kind out: nothing of it is ever pruned, missing entities included. :meth:`is_dead` is the same
        rule, missing entity included, for engines with no SQL, and the two must agree.
        """
        return None

    async def is_dead(self, entity_id: str) -> bool:
        """The in-process twin of :meth:`dead_lease_sql`: the entity is missing or finished.

        An adapter that cannot read its entity (it has no storage) answers False: the in-memory engine then
        KEEPS the lease, which is the conservative answer. It does not change what the SQL predicate says,
        which a Postgres engine evaluates in the database regardless of the adapter's storage.
        """
        return False


class ClaimEngine(ABC):
    # 01a0518b review: class-level default (not set in __init__) so
    # neither concrete engine's constructor needs to change - binding is
    # optional and post-construction, same shape as every other bind_*
    # setter this arc introduced. The engine itself never imports the
    # worker-layer function the hook eventually calls (durably_mark_
    # session_resumable) - the pool/factory closes over whatever
    # storage/engine references THAT needs and hands the engine only a
    # plain callable, keeping this module free of worker-layer imports.
    _post_release_hook: (
        "Callable[[PostReleaseWake], Awaitable[None]] | None"
    ) = None

    def bind_post_release_hook(
        self, hook: "Callable[[PostReleaseWake], Awaitable[None]] | None",
    ) -> None:
        """Bind the callable invoked after ``release()``'s own transaction
        commits, when an adapter's ``on_release`` returns a
        :class:`PostReleaseWake`. See that class's own docstring for why
        this exists instead of an adapter calling worker code directly.
        """
        self._post_release_hook = hook

    @abstractmethod
    async def claim_due(
        self, worker_id: str, *, max_count: int, kinds: list[ClaimKind] | None = None,
    ) -> list[Lease]:
        """Claim up to ``max_count`` due leases and return them.

        ``kinds``: restrict eligibility to this subset of adapters
        (default ``None`` = every registered kind, byte-identical to
        every existing caller). Phase 3 stage 7a (01a0518b) pool-class
        separation: the worker pool calls this twice per iteration when
        a reserved tool-call slice is configured — once excluding
        ``ClaimKind.TOOL_CALL`` for the general capacity, once
        restricted to it for the reserve — so a burst of tool-call
        tasks cannot starve session/harness/trigger claiming, or vice
        versa.
        """
        ...

    @abstractmethod
    async def heartbeat(
        self, worker_id: str, kind_ids: list[tuple[ClaimKind, str]],
    ) -> list[tuple[ClaimKind, str]]: ...

    @abstractmethod
    async def release(self, lease: Lease, *, outcome: ReleaseOutcome) -> None: ...

    @abstractmethod
    async def mark_resumable(
        self, kind: ClaimKind, entity_id: str, *, priority: int = CLAIM_PRIORITY_RESUME,
    ) -> None: ...

    @abstractmethod
    async def watch_ready(self) -> AsyncIterator[tuple[ClaimKind, str]]:
        """Yield ``(kind, entity_id)`` for each claim that just became ready.

        A wake-up hint only: claimers must still poll, because wakes sent while
        a watcher is down are lost. A backend whose subscription can be lost
        (a LISTEN connection) raises :class:`~primer.model.except_.ListenConnectionLost`
        when it is, after yielding everything received before the loss; the
        consumer re-subscribes by calling this again
        (``WorkerPool._engine_bus_loop`` does).
        """
        ...

    @abstractmethod
    async def upsert(
        self, kind: ClaimKind, entity_id: str, *, priority: int = CLAIM_PRIORITY_FRESH,
        next_attempt_at: datetime | None = None,
    ) -> None: ...

    @abstractmethod
    async def delete_lease(self, kind: ClaimKind, entity_id: str) -> None: ...

    async def has_lease(self, kind: ClaimKind, entity_id: str) -> bool:
        """Whether a lease ROW exists for ``(kind, entity_id)``: armed, claimed or expired.

        Distinct from :meth:`has_live_lease`, which is true only while a worker holds an
        unexpired claim. A row that exists is work in the queue (an unclaimed one waits for
        the next free worker, an expired one is reclaimed once its entity is eligible), so its
        entity is queued, not lost. The
        absence of a row is what a lost claim actually looks like. Answers "is this entity
        waiting for a worker?", which ``has_live_lease`` cannot: an armed lease nobody has
        claimed yet reads False there.

        Concrete, not abstract, and the default is ``True`` on purpose, for the same reason
        as :meth:`has_live_lease`: callers use this to decide whether something is stuck
        and may act destructively, so an engine that cannot answer must make them do
        nothing.
        """
        return True

    async def lease_exists(self, kind: ClaimKind, entity_ids: Sequence[str]) -> set[str]:
        """The subset of ``entity_ids`` that has a lease ROW of ``kind``: armed, claimed or expired.

        The batch form of :meth:`has_lease` (one query, not one per id), for a caller deciding which
        of a set of QUEUED entities still needs arming.

        Concrete, not abstract, and the default is ALL of ``entity_ids`` on purpose, for the same
        reason as :meth:`has_lease`: the caller acts on the ids that are NOT in the answer (it arms
        them), so an engine that cannot answer must make it do nothing.
        """
        return set(entity_ids)

    async def prune_dead_leases(self, kind: ClaimKind) -> int:
        """Delete lease rows of ``kind`` whose entity is missing or finished; return how many.

        "Dead" is the adapter's call (:meth:`ClaimAdapter.dead_lease_sql` / :meth:`~ClaimAdapter.is_dead`);
        a kind whose adapter does not define one is never pruned. A lease a worker currently holds
        (claimed and unexpired) is never deleted: its holder's release owns it. Concrete, with a
        default of ``0``: an engine that cannot answer prunes nothing.
        """
        return 0

    async def has_live_lease(self, kind: ClaimKind, entity_id: str) -> bool:
        """Whether a worker currently holds an unexpired lease on (kind, entity_id).

        This is the only honest "a turn is running right now" signal in the system.
        ``sessions.turn_no`` is not one: it is bumped on RELEASE, so a first turn that
        has been executing for hours still reads 0, indistinguishable from a turn that
        was never claimed.

        Concrete, not abstract, and the default is ``True`` on purpose. The sole caller
        is :class:`~primer.bus.scheduler_tasks.StuckSessionSweeper`, whose failure mode
        is ending a session out from under a live worker. An engine that cannot answer
        must therefore make the sweeper do nothing rather than make it reap.
        """
        return True
