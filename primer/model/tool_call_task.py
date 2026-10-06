"""ToolCallTask — scheduling state for one independently-claimable tool call.

Phase 3 stage 7a (docs/superpowers/2026-08-29-phase3-execution-topology-design.md,
user-approved; ground-truth remap in
docs/superpowers/2026-09-03-phase3-7a-ground-truth-remap.md). Every tool call
in a batch becomes its own claimable unit instead of executing sequentially
in-process, so parallel batches parallelize by worker availability and a
single gated call no longer blocks its siblings.

This row is SCHEDULING STATE ONLY (01a0518b Q1, resolved: ref, not inline).
The durable ``TOOL_CALL``/``TOOL_RESULT`` records in messages.jsonl remain
the transcript truth; this row never carries the tool's own execution
arguments or results inline, only enough to answer "is this done, and
what's its own lifecycle state" for the claim engine's eligibility
filter and the session's last-task-releases-re-arms-the-turn
bookkeeping. ``gate_state`` (below) is a narrow, ruling-approved
exception, not the row's general payload — see its own docstring.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from primer.model.common import Identifiable


def tool_call_task_id(session_id: str, scoped_call_id: str) -> str:
    """The row id of a task: ``<session_id>/<scoped_call_id>``.

    The scoped call id (``<node|x>:tool:<turn_no>:<seq>``, see ``primer.tap.delta.scoped_tool_call_id``) is unique
    within ONE session only: every agent session's first call of turn 0 is ``x:tool:0:1``. A task row id is a
    GLOBAL primary key and a lease key, so it is qualified with the session. The qualified form is INTERNAL (row
    id, lease ``entity_id``, park blobs, so ``parked_state`` as session reads serve it holds it); the transcript's
    TOOL_CALL/TOOL_RESULT records and every API field, UI view, MCP tool and channel payload a client acts on keep
    the scoped id (:func:`external_call_id` strips the qualification). Idempotent: an id that already carries
    this session's qualification is returned unchanged, so a seam that sees both fresh and carried-over entries
    can qualify them all.
    """
    prefix = f"{session_id}/"
    return scoped_call_id if scoped_call_id.startswith(prefix) else f"{prefix}{scoped_call_id}"


def external_call_id(task_id: str, session_id: str) -> str:
    """The scoped call id of ``task_id``: ``task_id`` without its ``<session_id>/`` qualification.

    The ONE place the qualification is stripped. A bare scoped id passes through unchanged (the prefix is matched
    exactly, never guessed from a ``/``, because graph node ids are free-form).
    """
    prefix = f"{session_id}/"
    return task_id[len(prefix):] if task_id.startswith(prefix) else task_id


class MalformedScopedIdError(ValueError):
    """A task id that is not ``[<session_id>/]<node>:tool:<turn_seg>:<seq>``. Handled by each caller, never guessed at."""


@dataclass(frozen=True)
class ScopedId:
    """The parts of a scoped tool-call id. ``turn_seg`` is the turn segment exactly as written in the id."""

    node: str
    turn_no: int
    epoch: int
    seq: int
    scoped: str
    turn_seg: str


# Canonical ASCII integers only: an epoch is written only when it is greater than zero, and a seq is 1-based.
# ``[0-9]``, not ``\d`` (which matches non-ASCII digits), and always with ``fullmatch`` (``$`` accepts a trailing newline).
_TURN_SEG = re.compile(r"(0|[1-9][0-9]*)(\.[1-9][0-9]*)?")
_SEQ = re.compile(r"[1-9][0-9]*")


def parse_scoped_task_id(task_id: str, session_id: str) -> ScopedId:
    """Parse a task id (session-qualified or bare scoped) into its parts. The ONE parser of the scoped-id shape.

    The ``<session_id>/`` qualification is stripped by :func:`external_call_id` (the one place it is stripped). The
    rest is split from the RIGHT (``rsplit(":", 3)``), because graph node ids are free-form and may contain ``:``
    or ``.``: the node keeps everything left of ``:tool:``. The turn segment is ``<turn_no>`` or
    ``<turn_no>.<epoch>`` (epoch > 0; the epoch form is ACCEPTED so a future writer's ids keep working, nothing
    writes it today: ``scoped_tool_call_id`` formats an int turn number only) and the seq is 1-based, both
    canonical ASCII integers; anything ``int()`` would also accept (a sign, whitespace, ``_``, a leading zero, a
    non-ASCII digit) is malformed. An empty node is malformed too: a graph node id has ``min_length=1`` and the
    agent surface mints ``x``.

    ``session_id`` is required: parsed without it, a qualified id would yield the node ``<session_id>/<node>`` and
    no error. Raises :class:`MalformedScopedIdError` naming the id.
    """
    scoped = external_call_id(task_id, session_id)
    parts = scoped.rsplit(":", 3)
    if len(parts) == 4 and parts[0] and parts[1] == "tool":
        node, _, turn_seg, seq = parts
        turn = _TURN_SEG.fullmatch(turn_seg)
        if turn is not None and _SEQ.fullmatch(seq) is not None:
            epoch = turn.group(2)
            return ScopedId(
                node=node,
                turn_no=int(turn.group(1)),
                epoch=int(epoch[1:]) if epoch else 0,
                seq=int(seq),
                scoped=scoped,
                turn_seg=turn_seg,
            )
    raise MalformedScopedIdError(
        f"malformed scoped tool-call id {task_id!r} (session {session_id!r}): "
        "expected [<session_id>/]<node>:tool:<turn_no>[.<epoch>]:<seq>"
    )


class ToolCallTaskState(StrEnum):
    """Mirrors the design doc's own enum exactly: queued|gated|running|done|failed."""

    QUEUED = "queued"
    GATED = "gated"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class ToolCallTask(Identifiable):
    """One independently-claimable tool call within a session turn.

    ``id`` is the SESSION-QUALIFIED scoped tool-call id
    (``<session_id>/<node>:tool:<turn_seg>:<seq>``, see
    :func:`tool_call_task_id`): the scoped id minted at the SAME dispatch seam
    that creates this row and that mints the durable ``TOOL_CALL`` record
    (unique within ONE session, and what the record carries as its own id),
    with the session in front because a row id is a global primary key and
    every agent session's first call of turn 0 is ``x:tool:0:1``.
    :attr:`scoped_call_id` returns the transcript form, and that is how the
    row and the record are joined. NOT autogenerated (no ``_id_prefix``): the
    dispatch seam always supplies it explicitly.
    """

    session_id: str = Field(..., min_length=1)
    turn_no: int = Field(..., ge=0)
    tool_name: str = Field(..., min_length=1)
    state: ToolCallTaskState = ToolCallTaskState.QUEUED

    # The durable TOOL_CALL record's own seq (primer.model.workspace_session.
    # SessionMessageRecord.seq) — a POINTER, not a copy, so Q1's ref-only
    # framing stays intact. REQUIRED (not optional): this is the field that
    # ENCODES the ordering invariant the whole design depends on. You
    # cannot know a record's seq until AFTER WorkspaceMessageWriter.append
    # durably writes it — so a ToolCallTask can only ever be CONSTRUCTED
    # with a real record_seq once its TOOL_CALL record already exists in
    # messages.jsonl. That ordering (record written, THEN task becomes
    # claimable) is exactly what a claim-based worker needs to be true: it
    # must never be able to claim a task whose TOOL_CALL record isn't
    # durable yet. The lookup helper that reads a task's arguments back
    # (primer.claim.tool_call_lookup.read_tool_call_record; it exists but
    # has no production caller, because the claim worker that would call
    # it is not built) seeks directly to
    # this seq and verifies the record's own id == this row's ``scoped_call_id``, failing
    # loudly on any mismatch rather than falling back to a scan — a
    # mismatch means this invariant broke somewhere upstream, which is a
    # bug worth surfacing, not papering over.
    record_seq: int = Field(..., ge=1)

    # The provider's own id for this call, the only id on the LLM wire: the assistant message in the parked history
    # carries it, so every ToolResultPart handed back to the model (a real result, a poison or orphan synthesis, the
    # coordinator's fallbacks) must carry it too. ``None`` on a row written before this field existed, which the
    # readers treat as "use the scoped id" (the previous behaviour).
    call_id: str | None = None

    # Every id (claimable + notifying) created from the SAME ToolWaitPark
    # batch as this row, THIS row's own id included (01a0518b, mixed-park
    # wake seam). The SAME list on every row in a batch - not a "first" /
    # "last" pick, so this stays consistent with the no-primary-projection
    # discipline pending_gates.py established. ToolCallClaimAdapter.
    # on_release's terminal branch reads every id here via individual
    # storage.get(id, conn=conn) calls (NOT Storage.find, which has no
    # conn param and would read outside this release's own transaction)
    # to determine "am I the last sibling to go terminal" - a genuinely
    # NEW piece of information each row needs (which OTHER ids share its
    # batch), unlike a wake KEY, which is a pure function of the row's own
    # id (see primer.session.yields.tool_wait_event_key) and is
    # deliberately NOT stored here for exactly that reason.
    batch_task_ids: list[str] = Field(default_factory=list)

    # Set only while state == GATED (approval-required or a yielding tool
    # mid-execution) — the gate's own event_key, so the resume path knows
    # which wake event to wait for. Mirrors WorkspaceSession.parked_event_key
    # in spirit, task-scoped instead of session-scoped so one gated call in
    # a batch does not block its siblings (the design's own motivating bug).
    gate_event_key: str | None = None
    gate_until: datetime | None = None

    # Set only while state == GATED - the SAME parked-state blob shape
    # session/graph parks already carry (yielded.resume_metadata.
    # original_call + policy_id/approval_type/gate_reason - see
    # primer.agent.approval_record.record_from_parked_blob, which reads
    # exactly this shape and is reused unmodified for the task-granular
    # ToolApprovalRecord write ruling 3's rider requires at resume).
    # This is the one narrow exception to "scheduling state only" (Q1):
    # ruling 3 (01a0518b) - task row stays pointer-only for the FINISHED
    # transcript truth (TOOL_CALL/TOOL_RESULT records), but a gate's live
    # payload has no other durable home at task granularity (unlike a
    # session park, which already had parked_state as its established
    # home; a bare-new ToolCallTask has nothing else to point at).
    # NOT cleared at resume (GATED -> QUEUED, see
    # primer.claim.tool_call_resume.durably_mark_tool_call_task_resumable):
    # the resume decision itself gets stashed in here too
    # (resume_event_payload/resume_event_key, mirroring
    # WorkspaceSession.parked_state's own resume-payload convention) so
    # the re-claiming worker has something to read the decision from.
    # Cleared to None only once actually consumed - on a terminal
    # release (DONE/FAILED) or a transient-failure requeue back to
    # QUEUED, see ToolCallClaimAdapter.on_release.
    gate_state: dict[str, Any] | None = None

    # Set only on a terminal (state in {DONE, FAILED}) release when the
    # underlying tool call itself failed - independent of whether the ROW's
    # own scheduling lifecycle completed successfully (it always does,
    # once terminal; "failed" describes the tool call, not the task
    # bookkeeping). The durable TOOL_RESULT record is the actual error
    # detail; this is a cheap, queryable summary. It is also what the model is shown for a FAILED task that has no
    # result (a poisoned task, or a handler's invalid release, which names the keys it got wrong).
    last_error: str | None = None

    # The tool-execution result (primer.model.chat.ToolResultPart's own
    # shape: output/error/metadata), NOT itself a durable TOOL_RESULT
    # record. Ruling (01a0518b, "same philosophy as gate_state"): the
    # SINGLE in-process session-log writer + its in-memory seq counter is
    # the shared, working invariant this design must not touch (a claim
    # worker allocating its own atomic seq would still race the
    # still-active original turn's stale in-memory counter) - so the
    # intended claim worker writes ONLY here + publishes the tick, and
    # the resume coordinator (primer.worker.tool_wait_resume_coordinator)
    # reads it to assemble the continuation message and then writes the
    # durable TOOL_RESULT records, in-process, under the session log's
    # existing single-writer lock, when the parked turn resumes. Nothing
    # clears it after that read; only ToolCallClaimAdapter.on_release's
    # retry branch resets it to None. Today the only production writers
    # are the park handlers, for notifying calls (born DONE with the
    # result already set); the claim worker that would write it for a
    # claimable call is not built.
    result_state: dict[str, Any] | None = None

    # Unclean executions so far. CONTRACT for the executor slice, which is not built (nothing
    # writes this today; the adapter only carries it through ``ReleaseOutcome.entity_update``):
    # a claim that finds the row RUNNING (the previous holder died mid-run) and an explicit
    # failed release count; a clean GATED release, a gate resume and a drain requeue do not.
    # The executor poisons the task when this reaches ``max_attempts``, and writes it as a
    # read-then-write from a FENCED read (``patch_if`` with the old value in ``where``), so a
    # concurrent increment rejects the write instead of being lost.
    attempts: int = Field(0, ge=0)

    # The per-CLAIM fence for every row write the executor makes. BUILT: ``ToolCallClaimAdapter.
    # on_release`` writes only while the row still carries the releaser's token, so a release
    # that outlived its claim cannot move a row someone else now owns, and ``None`` never
    # matches a fence. CONTRACT for the (unbuilt) claiming handler: it mints the token
    # (``worker:claimed_at:random``) and writes it with the RUNNING patch, which overwrites the
    # previous holder's token.
    claim_token: str | None = None

    # Bumped by every GATED release (BUILT: ``ToolCallClaimAdapter``, one fenced read of the
    # counter). CONTRACT for the decisions slice, not built: a REST/channel decision carries the
    # value the resolver returned and flips the gate only while it still matches, so a decision
    # for an earlier gate of the same call cannot resume a later one. Today the gated -> queued
    # flip (``durably_mark_tool_call_task_resumable``) compares only ``state != done``.
    gate_seq: int = Field(0, ge=0)

    # CONTRACT, not built (nothing stamps it today): the resume coordinator stamps it when it
    # persists this task's TOOL_RESULT record, and retention never prunes a terminal row that
    # has not been materialized.
    materialized_at: datetime | None = None

    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def scoped_call_id(self) -> str:
        """The id this call has in the transcript and on every surface a client acts on (``id`` without the session)."""
        return external_call_id(self.id, self.session_id)

    @property
    def wire_call_id(self) -> str:
        """The id the LLM knows this call by: the provider's raw id, else the scoped id for a legacy row."""
        return self.call_id or self.scoped_call_id


__all__ = [
    "MalformedScopedIdError",
    "ScopedId",
    "ToolCallTask",
    "ToolCallTaskState",
    "external_call_id",
    "parse_scoped_task_id",
    "tool_call_task_id",
]
