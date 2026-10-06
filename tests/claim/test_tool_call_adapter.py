"""Unit tests for ToolCallClaimAdapter (Phase 3 stage 7a, 01a0518b).

Mirrors tests/claim/test_session_adapter.py's shape (eligibility_sql, entity_indexes, on_release
scenarios against a storage double). The double is the shared in-memory ``Storage`` fake, which
implements ``patch_if`` with the same oracle the real backends are held to, so the adapter's fence is
exercised for real here and against SQLite in test_entity_storage_hooks.py.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from primer.claim.adapters.tool_calls import ToolCallClaimAdapter
from primer.int.claim import ClaimKind, ParkRequest, PostReleaseWake, ReleaseOutcome
from primer.model.chat import ToolResultPart
from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState
from tests.conftest import _InMemoryStorage


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _token(task_id: str) -> str:
    return f"tok-{task_id}"


def _make_task(
    task_id: str,
    *,
    state: ToolCallTaskState = ToolCallTaskState.RUNNING,
    gate_state: dict | None = None,
    batch_task_ids: list[str] | None = None,
    claim_token: str | None = "",
    **extra,
) -> ToolCallTask:
    """A task as a claiming handler leaves it: RUNNING, carrying ``tok-<id>`` (pass ``None`` for none)."""
    return ToolCallTask(
        id=task_id,
        session_id="sess-1",
        turn_no=0,
        tool_name="workspace__write",
        state=state,
        record_seq=1,
        gate_state=gate_state,
        batch_task_ids=batch_task_ids or [],
        claim_token=_token(task_id) if claim_token == "" else claim_token,
        created_at=_now(),
        started_at=_now(),
        **extra,
    )


def _release(task_id: str, **kwargs) -> ReleaseOutcome:
    """A release carrying the task's own token (override with ``claim_token=``)."""
    kwargs.setdefault("claim_token", _token(task_id))
    return ReleaseOutcome(**kwargs)


def _gate(event_key: str = "tool_approval:sess-1:t1", until: datetime | None = None) -> ParkRequest:
    return ParkRequest(
        parked_state={"kind": "approval"},
        parked_event_key=event_key,
        parked_until=until,
        parked_at=_now(),
    )


class FakeStorage(_InMemoryStorage[ToolCallTask]):
    """The shared in-memory ``Storage`` fake, preloaded, recording every ``get`` it serves."""

    def __init__(self, *tasks: ToolCallTask) -> None:
        super().__init__(ToolCallTask)
        for task in tasks:
            self._data[task.id] = task
        self.get_calls: list[str] = []

    async def get(self, id: str, *, conn=None) -> ToolCallTask | None:
        self.get_calls.append(id)
        return await super().get(id, conn=conn)


# ---------------------------------------------------------------------------
# Kind / eligibility / indexes
# ---------------------------------------------------------------------------


def test_tool_call_adapter_kind():
    a = ToolCallClaimAdapter(task_storage=None)
    assert a.kind is ClaimKind.TOOL_CALL
    assert a.entity_table == "toolcalltask"


def test_tool_call_eligibility_sql():
    a = ToolCallClaimAdapter(task_storage=None)
    sql = a.eligibility_sql()
    # state lives in the JSONB data column - a bare e.state reference
    # raises UndefinedColumnError on Postgres and breaks the WHOLE claim
    # loop, not just this kind (same footgun the session adapter's own
    # test guards against).
    assert "e.data->>'state'" in sql
    assert "e.state" not in sql
    assert "'queued'" in sql
    assert "'running'" in sql
    # Excluded: gated (task-granular gating - the whole point) and both
    # terminal states.
    assert "'gated'" not in sql
    assert "'done'" not in sql
    assert "'failed'" not in sql


def test_tool_call_entity_indexes_are_safe_to_repeat():
    a = ToolCallClaimAdapter(task_storage=None)
    ddl = a.entity_indexes('"public"."toolcalltask"')
    assert ddl, "expected at least one index statement"
    assert all(d.startswith("CREATE INDEX IF NOT EXISTS") for d in ddl)
    assert all('"public"."toolcalltask"' in d for d in ddl)
    joined = "\n".join(ddl)
    assert "(data->>'state')" in joined
    assert "(data->>'session_id')" in joined
    assert "(data->>'turn_no')" in joined


# ---------------------------------------------------------------------------
# on_release: gate branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_release_gate_sets_gated_state_and_event_key() -> None:
    storage = FakeStorage(_make_task("t1"))
    adapter = ToolCallClaimAdapter(task_storage=storage)

    until = _now()
    signal = await adapter.on_release(
        conn=None, entity_id="t1",
        outcome=_release("t1", success=False, drop_lease=True, park=_gate(until=until)),
    )
    assert signal is None
    updated = await storage.get("t1")
    assert updated.state == ToolCallTaskState.GATED
    assert updated.gate_event_key == "tool_approval:sess-1:t1"
    assert updated.gate_until == until
    assert updated.gate_state == {"kind": "approval"}
    assert updated.gate_seq == 1
    assert updated.claim_token is None, "a gated row has no owner; its token must die with the claim"


@pytest.mark.asyncio
async def test_each_gate_bumps_gate_seq_so_a_decision_for_an_earlier_gate_cannot_resume_a_later_one() -> None:
    storage = FakeStorage(_make_task("t1"))
    adapter = ToolCallClaimAdapter(task_storage=storage)

    for expected_seq in (1, 2, 3):
        await adapter.on_release(
            conn=None, entity_id="t1",
            outcome=_release("t1", success=False, drop_lease=True, park=_gate()),
        )
        gated = await storage.get("t1")
        assert (gated.state, gated.gate_seq) == (ToolCallTaskState.GATED, expected_seq)
        # the gate flips back to QUEUED and the next claim stamps a fresh token
        await storage.update(gated.model_copy(update={
            "state": ToolCallTaskState.RUNNING, "claim_token": _token("t1"),
        }))


@pytest.mark.asyncio
async def test_a_gate_release_is_rejected_when_gate_seq_moved_between_the_read_and_the_write() -> None:
    """gate_seq is a counter: the adapter reads it and writes ``old + 1`` naming ``old`` in the fence,
    so a concurrent change rejects the write instead of being lost (mutation: drop gate_seq from where)."""

    class _MovesUnderneath(FakeStorage):
        async def get(self, id, *, conn=None):
            row = await super().get(id, conn=conn)
            if row is not None and row.gate_seq == 0:                    # another writer gets in first
                self._data[id] = row.model_copy(update={"gate_seq": 7})
            return row

    storage = _MovesUnderneath(_make_task("t1"))
    adapter = ToolCallClaimAdapter(task_storage=storage)
    await adapter.on_release(
        conn=None, entity_id="t1", outcome=_release("t1", success=False, drop_lease=True, park=_gate()),
    )
    row = await storage.get("t1")
    assert (row.state, row.gate_seq) == (ToolCallTaskState.RUNNING, 7), "the stale gate write landed"


@pytest.mark.asyncio
async def test_a_row_stored_before_gate_seq_existed_still_gates() -> None:
    """The park handlers on main write rows without the key; the model reads gate_seq as 0 and the fence names 0,
    which patch_if lets match the absent key. Without that, the gate write was a silent no-op: the row stayed
    RUNNING with its token after the lease was already dropped."""
    storage = FakeStorage()
    legacy = _make_task("t1").model_dump(mode="json")
    for key in ("gate_seq", "attempts", "materialized_at"):
        del legacy[key]
    legacy.pop("id")
    storage.seed_raw("t1", legacy)
    adapter = ToolCallClaimAdapter(task_storage=storage)

    await adapter.on_release(
        conn=None, entity_id="t1", outcome=_release("t1", success=False, drop_lease=True, park=_gate()),
    )
    gated = await storage.get("t1")
    assert (gated.state, gated.gate_seq) == (ToolCallTaskState.GATED, 1)


# ---------------------------------------------------------------------------
# on_release: terminal branch (drop_lease=True)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_release_terminal_success_sets_done() -> None:
    # gate_state seeded non-None: a call that was gated earlier in its
    # life, then resumed and ran to completion, must not leave a stale
    # gate payload behind on the terminal row.
    # batch_task_ids empty (not part of a tool_wait batch): no sibling
    # check, on_release returns None - a lone claimed call outside the
    # claim-based dispatch seam.
    task = _make_task("t2", gate_state={"resume_event_payload": {"decision": "approved"}})
    storage = FakeStorage(task)
    adapter = ToolCallClaimAdapter(task_storage=storage)

    result = await adapter.on_release(
        conn=None, entity_id="t2", outcome=_release("t2", success=True, drop_lease=True),
    )
    assert result is None
    updated = await storage.get("t2")
    assert updated.state == ToolCallTaskState.DONE
    assert updated.finished_at is not None
    assert updated.last_error is None
    assert updated.gate_state is None


@pytest.mark.asyncio
async def test_on_release_terminal_failure_sets_failed_with_error() -> None:
    """A poisoned task (fail-count cap exceeded upstream): the caller has
    already written the failed TOOL_RESULT record and releases with
    success=False, drop_lease=True to signal "stop retrying, this is
    terminal" rather than requeuing it forever."""
    storage = FakeStorage(_make_task("t3"))
    adapter = ToolCallClaimAdapter(task_storage=storage)

    await adapter.on_release(
        conn=None, entity_id="t3",
        outcome=_release("t3", success=False, drop_lease=True, last_error="tool raised OSError"),
    )
    updated = await storage.get("t3")
    assert updated.state == ToolCallTaskState.FAILED
    assert updated.finished_at is not None
    assert updated.last_error == "tool raised OSError"


@pytest.mark.asyncio
async def test_terminal_release_applies_entity_update_in_the_same_write() -> None:
    storage = FakeStorage(_make_task("t2", attempts=1))
    adapter = ToolCallClaimAdapter(task_storage=storage)

    await adapter.on_release(
        conn=None, entity_id="t2",
        outcome=_release(
            "t2", success=True, drop_lease=True,
            entity_update={"result_state": ToolResultPart(id="t2", output="done"), "attempts": 2},
        ),
    )
    updated = await storage.get("t2")
    assert updated.state == ToolCallTaskState.DONE
    assert updated.attempts == 2
    assert updated.result_state is not None
    assert (updated.result_state["id"], updated.result_state["output"]) == ("t2", "done")


@pytest.mark.asyncio
async def test_a_poisoned_task_fails_with_its_synthesised_error_result() -> None:
    storage = FakeStorage(_make_task("t2", attempts=5))
    adapter = ToolCallClaimAdapter(task_storage=storage)
    poison = ToolResultPart(id="t2", output='{"error": "tool call failed after 5 attempts"}', error=True)

    await adapter.on_release(
        conn=None, entity_id="t2",
        outcome=_release(
            "t2", success=False, drop_lease=True, last_error="poisoned",
            entity_update={"result_state": poison},
        ),
    )
    updated = await storage.get("t2")
    assert (updated.state, updated.last_error) == (ToolCallTaskState.FAILED, "poisoned")
    assert updated.result_state["error"] is True


# ---------------------------------------------------------------------------
# on_release: retryable branch (drop_lease=False, not gated)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_release_retryable_failure_resets_to_queued() -> None:
    """A transient failure (reclaim, worker crash) - not terminal, not
    gated - resets to QUEUED so the next claim (this worker or another)
    picks it up again. The engine's own lease.attempt_count is the
    authoritative retry counter for the lease; this row just stops
    reading RUNNING once nobody is actually running it."""
    task = _make_task(
        "t4", gate_state={"resume_event_payload": {"decision": "approved"}},
        result_state={"id": "t4", "output": "from a run that never released cleanly"},
        attempts=1,
    )
    storage = FakeStorage(task)
    adapter = ToolCallClaimAdapter(task_storage=storage)

    await adapter.on_release(
        conn=None, entity_id="t4", outcome=_release("t4", success=False, last_error="reclaim"),
    )
    updated = await storage.get("t4")
    assert updated.state == ToolCallTaskState.QUEUED
    assert updated.started_at is None
    assert updated.gate_state is None
    assert updated.result_state is None
    assert updated.claim_token is None, "a requeued row has no owner"
    assert updated.attempts == 1, "a bare requeue (drain, reclaim) does not count as an unclean attempt"


@pytest.mark.asyncio
async def test_retry_release_applies_entity_update_attempts_and_last_error() -> None:
    storage = FakeStorage(_make_task("t4", attempts=1))
    adapter = ToolCallClaimAdapter(task_storage=storage)

    await adapter.on_release(
        conn=None, entity_id="t4",
        outcome=_release(
            "t4", success=False,
            entity_update={"attempts": 2, "last_error": "OSError: disk full"},
        ),
    )
    updated = await storage.get("t4")
    assert (updated.state, updated.attempts, updated.last_error) == (
        ToolCallTaskState.QUEUED, 2, "OSError: disk full",
    )


# ---------------------------------------------------------------------------
# The fence: state AND per-claim token, one patch_if, no preceding get (S1-(3), N6, N11)
# ---------------------------------------------------------------------------

_BRANCHES = {
    "gate": dict(success=False, drop_lease=True, park=_gate()),
    "terminal_ok": dict(success=True, drop_lease=True),
    "terminal_fail": dict(success=False, drop_lease=True, last_error="boom"),
    "retry": dict(success=False),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", sorted(_BRANCHES))
async def test_token_less_release_does_not_fail_a_fresh_queued_row(branch, caplog) -> None:
    """A QUEUED row has no owner (token None). A release that carries no token must not match it as
    'absent or null' and write it: it would FAIL a fresh row nobody ran (mutation N11)."""
    fresh = _make_task("t1", state=ToolCallTaskState.QUEUED, claim_token=None)
    storage = FakeStorage(fresh)
    adapter = ToolCallClaimAdapter(task_storage=storage)

    with caplog.at_level(logging.WARNING, logger="primer.claim.adapters.tool_calls"):
        signal = await adapter.on_release(
            conn=None, entity_id="t1", outcome=ReleaseOutcome(claim_token=None, **_BRANCHES[branch]),
        )
    assert signal is None
    assert await storage.get("t1") == fresh, "a token-less release wrote the row"
    assert any("no claim token" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", sorted(_BRANCHES))
async def test_zombie_release_does_not_land_after_same_worker_reclaim(branch, caplog) -> None:
    """A re-claim stamped a NEW token on the row; the previous holder's release (the old token) arrives
    late and must write nothing (mutation N6: drop the claim_token predicate)."""
    reclaimed = _make_task("t1", claim_token="tok-B")
    storage = FakeStorage(reclaimed)
    adapter = ToolCallClaimAdapter(task_storage=storage)

    with caplog.at_level(logging.WARNING, logger="primer.claim.adapters.tool_calls"):
        signal = await adapter.on_release(
            conn=None, entity_id="t1", outcome=ReleaseOutcome(claim_token="tok-A", **_BRANCHES[branch]),
        )
    assert signal is None
    assert await storage.get("t1") == reclaimed, "a stale release moved a row it no longer owns"
    assert any("rejected by its fence" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [ToolCallTaskState.GATED, ToolCallTaskState.DONE, ToolCallTaskState.FAILED])
@pytest.mark.parametrize("branch", sorted(_BRANCHES))
async def test_a_release_never_moves_a_gated_or_finished_row(state, branch) -> None:
    """Same token, wrong state: a row cancelled, orphaned or already finished underneath the releaser."""
    row = _make_task("t1", state=state)
    storage = FakeStorage(row)
    adapter = ToolCallClaimAdapter(task_storage=storage)

    signal = await adapter.on_release(conn=None, entity_id="t1", outcome=_release("t1", **_BRANCHES[branch]))
    assert signal is None
    assert await storage.get("t1") == row


@pytest.mark.asyncio
async def test_a_rejected_terminal_release_fires_no_wake_even_for_the_last_sibling() -> None:
    batch = ["b:tool:0:1", "b:tool:0:2"]
    zombie = _make_task("b:tool:0:1", batch_task_ids=batch, claim_token="tok-B")
    done = _make_task("b:tool:0:2", state=ToolCallTaskState.DONE, batch_task_ids=batch)
    storage = FakeStorage(zombie, done)
    adapter = ToolCallClaimAdapter(task_storage=storage)

    signal = await adapter.on_release(
        conn=None, entity_id="b:tool:0:1",
        outcome=ReleaseOutcome(success=True, drop_lease=True, claim_token="tok-A"),
    )
    assert signal is None


@pytest.mark.asyncio
async def test_a_stale_token_release_after_a_gated_park_is_a_no_op() -> None:
    """The gated branch clears the claim token (a gated row has no owner). The old holder's token must therefore
    fail the fence on a second release, and again after the gate flips the row back to QUEUED, until the next claim
    stamps a new token (mutation: leave the token on the gated row)."""
    storage = FakeStorage(_make_task("t1"))
    adapter = ToolCallClaimAdapter(task_storage=storage)
    gate = _release("t1", success=False, drop_lease=True, park=_gate())
    await adapter.on_release(conn=None, entity_id="t1", outcome=gate)
    gated = await storage.get("t1")
    assert (gated.state, gated.claim_token) == (ToolCallTaskState.GATED, None)

    for late in (_release("t1", success=True, drop_lease=True), _release("t1", success=False), gate):
        assert await adapter.on_release(conn=None, entity_id="t1", outcome=late) is None
        assert await storage.get("t1") == gated, "a stale release moved a gated row"

    flipped = gated.model_copy(update={"state": ToolCallTaskState.QUEUED})          # the gate resolved
    await storage.update(flipped)
    assert await adapter.on_release(
        conn=None, entity_id="t1", outcome=_release("t1", success=True, drop_lease=True),
    ) is None
    assert await storage.get("t1") == flipped, "a stale release finished a re-queued row it no longer owns"


@pytest.mark.asyncio
async def test_a_duplicate_terminal_release_of_a_finished_row_fires_no_second_wake() -> None:
    """The old whole-document write re-fired the wake on every terminal release. The row is already DONE with this
    token, every sibling is DONE, and the batch is complete: a second release must be rejected and return no wake."""
    batch = ["b:tool:0:1", "b:tool:0:2"]
    first = _make_task("b:tool:0:1", batch_task_ids=batch)
    sibling = _make_task("b:tool:0:2", state=ToolCallTaskState.DONE, batch_task_ids=batch)
    storage = FakeStorage(first, sibling)
    adapter = ToolCallClaimAdapter(task_storage=storage)
    release = _release("b:tool:0:1", success=True, drop_lease=True)

    assert await adapter.on_release(conn=None, entity_id="b:tool:0:1", outcome=release) is not None
    assert await adapter.on_release(conn=None, entity_id="b:tool:0:1", outcome=release) is None


@pytest.mark.asyncio
async def test_the_release_is_one_fenced_write_not_a_read_then_an_unconditional_update() -> None:
    """Terminal and retry branches do not read the row first: a read here would be the window a
    competing writer slips through. (The gate branch reads once, for its counter.)"""
    storage = FakeStorage(_make_task("t1"), _make_task("t2"))
    adapter = ToolCallClaimAdapter(task_storage=storage)

    await adapter.on_release(conn=None, entity_id="t1", outcome=_release("t1", success=False))
    await adapter.on_release(conn=None, entity_id="t2", outcome=_release("t2", success=True, drop_lease=True))
    assert storage.get_calls == [], "a non-gate release read the row before writing it"


@pytest.mark.asyncio
async def test_an_unknown_entity_update_key_fails_the_task_terminally_instead_of_raising() -> None:
    """``state``, ``claim_token``, the gate fields and ``finished_at`` belong to the branches; a handler must not
    be able to write them (the adapter stamps ``finished_at`` itself). Raising inside the release transaction would roll
    the release back and leave the lease claimed until it expires, for the same buggy handler to repeat for ever, so the
    task is failed TERMINALLY with the reason as its ``last_error``, the offending value is never written, and a
    sibling's last-sibling wake still fires. Two tasks of one batch: the second is untouched by the first's rejection."""
    for key in ("state", "claim_token", "gate_seq", "id", "finished_at", "materialized_at"):
        batch = ["b:tool:0:1", "b:tool:0:2"]
        first = _make_task("b:tool:0:1", batch_task_ids=batch)
        sibling = _make_task("b:tool:0:2", state=ToolCallTaskState.DONE, batch_task_ids=batch)
        storage = FakeStorage(first, sibling)
        adapter = ToolCallClaimAdapter(task_storage=storage)

        wake = await adapter.on_release(
            conn=None, entity_id="b:tool:0:1",
            outcome=_release("b:tool:0:1", success=True, drop_lease=True, entity_update={key: "x"}),
        )

        failed = await storage.get("b:tool:0:1")
        assert failed.state == ToolCallTaskState.FAILED, key
        assert failed.last_error is not None and f"it set ['{key}']" in failed.last_error, failed.last_error
        assert "Adapter" not in failed.last_error, "the model-visible reason carries no internal class names"
        assert failed.result_state is None, "the rejected update must not be applied"
        assert isinstance(wake, PostReleaseWake), "the batch is complete, so the session must be woken"
        assert await storage.get("b:tool:0:2") == sibling, "a sibling's row was touched"


@pytest.mark.asyncio
async def test_the_entity_update_allow_list_is_per_branch_and_a_violation_fails_the_task() -> None:
    """A requeue clears result_state on purpose (a result from a run that never released cleanly must not survive
    into the next attempt), so it may not supply one; a clean gate changes neither result nor attempts. Each violation
    fails the task terminally, with the branch named in the reason."""
    result = ToolResultPart(id="t1", output="x")
    cases = [
        (dict(success=False, entity_update={"result_state": result}), "retry", "result_state"),
        (dict(success=False, drop_lease=True, park=_gate(), entity_update={"attempts": 2}), "gated", "attempts"),
    ]
    for kwargs, branch, key in cases:
        storage = FakeStorage(_make_task("t1"))
        adapter = ToolCallClaimAdapter(task_storage=storage)
        await adapter.on_release(conn=None, entity_id="t1", outcome=_release("t1", **kwargs))
        failed = await storage.get("t1")
        assert failed.state == ToolCallTaskState.FAILED, branch
        assert f"on a {branch} release, which takes only" in failed.last_error
        assert f"it set ['{key}']" in failed.last_error
        assert failed.attempts == 0 and failed.gate_event_key is None, "the rejected update or gate was applied"
    # the terminal branch takes all three
    storage = FakeStorage(_make_task("t1"))
    adapter = ToolCallClaimAdapter(task_storage=storage)
    await adapter.on_release(
        conn=None, entity_id="t1",
        outcome=_release("t1", success=False, drop_lease=True, entity_update={
            "result_state": result, "attempts": 3, "last_error": "poisoned",
        }),
    )
    done = await storage.get("t1")
    assert (done.state, done.attempts, done.last_error) == (ToolCallTaskState.FAILED, 3, "poisoned")


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs, key", [
    (dict(success=True, drop_lease=True, entity_update={"attempts": -1}), "attempts"),
    (dict(success=True, drop_lease=True, entity_update={"result_state": "not an object"}), "result_state"),
    (dict(success=True, drop_lease=True, entity_update={"last_error": {"a": 1}}), "last_error"),
    (dict(success=False, entity_update={"attempts": "many"}), "attempts"),
])
async def test_a_value_the_model_refuses_fails_the_task_too_not_just_a_disallowed_key(kwargs, key) -> None:
    """The allowed KEYS can still carry an unusable VALUE (``attempts=-1``, a ``result_state`` that is not an object).
    The merged document then fails validation (a pydantic ``ValidationError``) INSIDE the release transaction, which on
    Postgres is re-raised as itself and rolls the release back, so the same loop as a disallowed key. It is the same
    terminal failure, with a reason that names the key but carries neither the value nor pydantic's text."""
    storage = FakeStorage(_make_task("t1"))
    adapter = ToolCallClaimAdapter(task_storage=storage)

    await adapter.on_release(conn=None, entity_id="t1", outcome=_release("t1", **kwargs))

    failed = await storage.get("t1")
    assert failed.state == ToolCallTaskState.FAILED
    assert failed.last_error == f"the worker's release was invalid: it gave an invalid value for ['{key}']"
    assert failed.attempts == 0 and failed.result_state is None, "the refused value was applied"


_LONE_SURROGATE = "\ud800"


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [
    dict(success=True, drop_lease=True, entity_update={
        "result_state": ToolResultPart(id="t1", output=f"half a pair: {_LONE_SURROGATE}"),
    }),
    dict(success=False, drop_lease=True, entity_update={"last_error": _LONE_SURROGATE}),
    dict(success=False, drop_lease=True, last_error=f"boom {_LONE_SURROGATE}"),
    dict(success=False, entity_update={"last_error": _LONE_SURROGATE}),
    dict(success=False, drop_lease=True, park=ParkRequest(
        parked_state={"prompt": _LONE_SURROGATE}, parked_event_key="tool_approval:sess-1:t1", parked_until=None,
        parked_at=_now(),
    )),
], ids=["terminal-result_state", "terminal-entity-last_error", "terminal-outcome-last_error", "retry-last_error",
        "gate-parked_state"])
async def test_a_value_no_backend_can_store_fails_the_task_instead_of_escaping_the_release(kwargs) -> None:
    """A lone surrogate in anything the handler's release supplies is refused by the patch layer (``PatchSpecError``,
    before any I/O) on every backend. It used to escape ``on_release``, roll the release back and leave the lease to
    be claimed and released the same way again for ever; it now fails the task like any other rejected release, and
    the reason names no value."""
    storage = FakeStorage(_make_task("t1"))
    adapter = ToolCallClaimAdapter(task_storage=storage)

    await adapter.on_release(conn=None, entity_id="t1", outcome=_release("t1", **kwargs))

    failed = await storage.get("t1")
    assert failed.state == ToolCallTaskState.FAILED
    assert failed.last_error == (
        "the worker's release was invalid: it gave a value that cannot be stored as JSON "
        "(a non-finite number, or text that is not valid Unicode)"
    )
    assert failed.result_state is None and failed.gate_event_key is None, "the refused value was applied"


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [
    dict(success=True, drop_lease=True, entity_update={"last_error": object()}),
    dict(success=True, drop_lease=True, entity_update={"last_error": b"\xff"}),
    dict(success=False, drop_lease=True, last_error=b"\xff"),
    dict(success=False, entity_update={"last_error": object()}),
    dict(success=False, drop_lease=True, park=ParkRequest(
        parked_state={"prompt": object()}, parked_event_key="tool_approval:sess-1:t1", parked_until=None,
        parked_at=_now(),
    )),
], ids=["terminal-object", "terminal-bytes", "terminal-outcome-last_error-bytes", "retry-object", "gate-object"])
async def test_a_value_json_cannot_encode_fails_the_task_instead_of_escaping_the_release(kwargs) -> None:
    """The other family of unstorable values: one JSON has no form for (an arbitrary object, bytes that are not UTF-8).
    ``to_jsonable_python`` raises ``PydanticSerializationError`` / ``UnicodeDecodeError`` BEFORE ``patch_if`` is called,
    so the ``PatchValueError`` catch never saw it and it escaped ``on_release``: the release rolled back and the lease
    was claimed and released the same way again for ever. It now fails the task like the surrogate and the non-finite
    number, with a reason that names no value."""
    storage = FakeStorage(_make_task("t1"))
    adapter = ToolCallClaimAdapter(task_storage=storage)

    await adapter.on_release(conn=None, entity_id="t1", outcome=_release("t1", **kwargs))

    failed = await storage.get("t1")
    assert failed.state == ToolCallTaskState.FAILED
    assert failed.last_error == (
        "the worker's release was invalid: it gave a value that cannot be encoded as JSON "
        "(an object JSON has no form for, or bytes that are not valid UTF-8)"
    )
    assert failed.result_state is None and failed.gate_event_key is None, "the refused value was applied"


@pytest.mark.asyncio
async def test_an_unstorable_claim_token_leaves_the_row_alone_and_raises_out_of_the_release() -> None:
    """The one unstorable value the failure path cannot absorb is the claim token itself: ``_fail_rejected`` fences its
    own write on the same token, so that write is refused too and the ``_InvalidEntityUpdate`` leaves ``on_release``
    (the release fails, the row is left as it was and the lease stays claimed until it expires)."""
    from primer.claim.adapters.tool_calls import _InvalidEntityUpdate

    task = _make_task("t1", claim_token=_LONE_SURROGATE)
    storage = FakeStorage(task)
    adapter = ToolCallClaimAdapter(task_storage=storage)
    before = await storage.get("t1")

    with pytest.raises(_InvalidEntityUpdate):
        await adapter.on_release(
            conn=None, entity_id="t1",
            outcome=ReleaseOutcome(success=True, drop_lease=True, claim_token=_LONE_SURROGATE),
        )

    assert await storage.get("t1") == before, "a release whose token cannot be stored wrote something"


@pytest.mark.asyncio
async def test_the_invalid_release_log_does_not_claim_a_failure_its_fence_refused(caplog) -> None:
    """``_fail_rejected`` logs BEFORE its write. A stale claim token (the task was taken over) refuses that write, so
    the task is NOT failed: the ERROR line must say the attempt was made, not that the task is failed."""
    storage = FakeStorage(_make_task("t1"))
    adapter = ToolCallClaimAdapter(task_storage=storage)
    before = await storage.get("t1")

    with caplog.at_level(logging.ERROR, logger="primer.claim.adapters.tool_calls"):
        await adapter.on_release(
            conn=None, entity_id="t1",
            outcome=ReleaseOutcome(
                success=True, drop_lease=True, claim_token="tok-of-another-claim", entity_update={"attempts": 1, "bogus": 2},
            ),
        )

    assert await storage.get("t1") == before, "a stale release failed the task"
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "the invalid release was not logged"
    assert not any("is failed terminally" in m for m in errors), errors
    assert any("failing the task terminally" in m for m in errors), errors


@pytest.mark.asyncio
async def test_a_malformed_spec_the_adapter_built_itself_propagates_and_is_not_blamed_on_the_handler(monkeypatch) -> None:
    """Only a VALUE the patch layer refuses (``PatchValueError``) is the handler's doing. Any other ``PatchSpecError`` is a
    spec the adapter itself built wrongly (here: a field renamed on the way to ``patch_if``, as a typo in the adapter
    would), a bug that must surface, not be reported to the model as the handler's unstorable value with the task
    failed. Control: an unstorable value in the same retry release still fails the task."""
    from pydantic_core import to_jsonable_python as real_to_jsonable

    from primer.storage import PatchSpecError, PatchValueError

    def with_a_typo(value):
        out = real_to_jsonable(value)
        if isinstance(out, dict) and "started_at" in out:        # only the retry branch's patch writes started_at
            out["started_at_typo"] = out.pop("started_at")
        return out

    monkeypatch.setattr("primer.claim.adapters.tool_calls.to_jsonable_python", with_a_typo)
    storage = FakeStorage(_make_task("t1"))
    adapter = ToolCallClaimAdapter(task_storage=storage)
    before = await storage.get("t1")

    with pytest.raises(PatchSpecError) as excinfo:
        await adapter.on_release(conn=None, entity_id="t1", outcome=_release("t1", success=False))
    assert not isinstance(excinfo.value, PatchValueError)
    assert "started_at_typo" in str(excinfo.value)
    assert await storage.get("t1") == before, "the adapter's own bug failed the task"

    monkeypatch.undo()
    await adapter.on_release(
        conn=None, entity_id="t1", outcome=_release("t1", success=False, entity_update={"last_error": _LONE_SURROGATE}),
    )
    failed = await storage.get("t1")
    assert failed.state == ToolCallTaskState.FAILED
    assert failed.last_error.startswith("the worker's release was invalid: it gave a value that cannot be stored")


@pytest.mark.asyncio
async def test_a_validation_error_with_no_handler_values_is_a_corrupt_row_and_still_propagates() -> None:
    """With nothing supplied by the handler the unreadable document is the STORED row's doing, which is not the handler's
    to answer for: it propagates exactly as before instead of failing a task for a fault it did not cause."""
    class _CorruptRow(FakeStorage):
        async def patch_if(self, *a, **k):
            ToolCallTask.model_validate({})        # raises pydantic.ValidationError

    adapter = ToolCallClaimAdapter(task_storage=_CorruptRow(_make_task("t1")))
    with pytest.raises(Exception) as excinfo:
        await adapter.on_release(
            conn=None, entity_id="t1", outcome=_release("t1", success=True, drop_lease=True),
        )
    assert type(excinfo.value).__name__ == "ValidationError"


@pytest.mark.asyncio
async def test_the_engine_drops_the_lease_of_a_terminal_rejection_and_requeues_a_retry_rejections_which_prune_removes() -> None:
    """Engine level, not just the adapter: a rejected TERMINAL release drops the lease with the task FAILED; a rejected RETRY
    release requeues a lease whose task is FAILED (the in-memory engine applies no eligibility, so it is claimable again
    there; the handler's state guard is what stops it re-running), and ``prune_dead_leases`` removes it."""
    from primer.claim.in_memory import InMemoryClaimEngine

    storage = FakeStorage(_make_task("term"), _make_task("retry"))
    engine = InMemoryClaimEngine(adapters={ClaimKind.TOOL_CALL: ToolCallClaimAdapter(task_storage=storage)})
    for tid in ("term", "retry"):
        await engine.upsert(ClaimKind.TOOL_CALL, tid)
    leases = {l.entity_id: l for l in await engine.claim_due("w", max_count=10, kinds=[ClaimKind.TOOL_CALL])}

    await engine.release(leases["term"], outcome=_release(
        "term", success=True, drop_lease=True, entity_update={"state": "done"},
    ))
    await engine.release(leases["retry"], outcome=_release(
        "retry", success=False, entity_update={"result_state": ToolResultPart(id="retry", output="x")},
    ))

    assert (await storage.get("term")).state == ToolCallTaskState.FAILED
    assert (await storage.get("retry")).state == ToolCallTaskState.FAILED
    assert await engine.has_lease(ClaimKind.TOOL_CALL, "term") is False, "a terminal rejection drops its lease"
    assert await engine.has_lease(ClaimKind.TOOL_CALL, "retry") is True, "a retry rejection requeues a lease"
    assert await engine.prune_dead_leases(ClaimKind.TOOL_CALL) == 1
    assert await engine.has_lease(ClaimKind.TOOL_CALL, "retry") is False


@pytest.mark.asyncio
async def test_a_rejected_release_is_fenced_like_any_other_it_cannot_fail_another_claims_row() -> None:
    """The terminal failure goes through the same fence (live state AND the claim token): a stale handler whose release is
    both late and malformed must not fail the row of the claim that took over, and a token-less one writes nothing."""
    taken_over = _make_task("t1")        # the live claim holds ``_token("t1")``
    storage = FakeStorage(taken_over)
    adapter = ToolCallClaimAdapter(task_storage=storage)

    stale = ReleaseOutcome(
        success=True, drop_lease=True, claim_token="a-previous-claims-token", entity_update={"state": "done"},
    )
    assert await adapter.on_release(conn=None, entity_id="t1", outcome=stale) is None
    assert await storage.get("t1") == taken_over, "a stale malformed release failed the new claim's row"

    # a token-less malformed release: against a row that holds a token AND against a fresh QUEUED row that holds none
    # (a ``None`` in a fence matches "absent or null", so the fresh row is where a missing guard would show)
    fresh = _make_task("t2", state=ToolCallTaskState.QUEUED, claim_token=None)
    storage = FakeStorage(taken_over, fresh)
    adapter = ToolCallClaimAdapter(task_storage=storage)
    tokenless = ReleaseOutcome(success=True, drop_lease=True, claim_token=None, entity_update={"state": "done"})
    assert await adapter.on_release(conn=None, entity_id="t1", outcome=tokenless) is None
    assert await adapter.on_release(conn=None, entity_id="t2", outcome=tokenless) is None
    assert await storage.get("t1") == taken_over, "a token-less malformed release wrote a row"
    assert await storage.get("t2") == fresh, "a token-less malformed release failed a fresh QUEUED row"


# ---------------------------------------------------------------------------
# on_release: missing storage / missing row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_release_raises_without_storage() -> None:
    adapter = ToolCallClaimAdapter(task_storage=None)
    with pytest.raises(RuntimeError, match="task_storage is None"):
        await adapter.on_release(
            conn=None, entity_id="ghost",
            outcome=ReleaseOutcome(success=True, drop_lease=True, claim_token="tok"),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", sorted(_BRANCHES))
async def test_on_release_no_op_when_row_missing(branch) -> None:
    storage = FakeStorage(_make_task("t5"))
    adapter = ToolCallClaimAdapter(task_storage=storage)

    signal = await adapter.on_release(
        conn=None, entity_id="does-not-exist",
        outcome=ReleaseOutcome(claim_token="tok", **_BRANCHES[branch]),
    )
    assert signal is None
    assert await storage.get("t5") is not None


# ---------------------------------------------------------------------------
# dead leases: the adapter's half of ClaimEngine.prune_dead_leases
# ---------------------------------------------------------------------------


def test_dead_lease_sql_names_the_finished_states_and_not_gated() -> None:
    sql = ToolCallClaimAdapter(task_storage=None).dead_lease_sql()
    assert sql is not None
    assert "e.data->>'state'" in sql
    assert "'done'" in sql and "'failed'" in sql
    assert "'gated'" not in sql, "a gated task is waiting on a gate, not finished"
    assert "'queued'" not in sql and "'running'" not in sql


@pytest.mark.asyncio
@pytest.mark.parametrize("state", list(ToolCallTaskState))
async def test_is_dead_agrees_with_the_sql_predicate_for_every_state(state) -> None:
    """Parametrised over the ENUM, not a hand-written list: a state added later must be placed here on purpose,
    never default to dead (a dead lease is deleted by prune_dead_leases)."""
    finished = {ToolCallTaskState.DONE, ToolCallTaskState.FAILED}
    adapter = ToolCallClaimAdapter(task_storage=FakeStorage(_make_task("t1", state=state)))
    assert await adapter.is_dead("t1") is (state in finished), f"{state}: dead means finished, nothing else"
    sql = adapter.dead_lease_sql()
    assert (f"'{state.value}'" in sql) is (state in finished), f"{state}: SQL and in-process rule disagree"
    assert await adapter.is_dead("missing") is True, "a task row that is gone is dead"


@pytest.mark.asyncio
async def test_is_dead_without_storage_says_no() -> None:
    assert await ToolCallClaimAdapter(task_storage=None).is_dead("t1") is False


# ---------------------------------------------------------------------------
# on_release: last-sibling wake signal (01a0518b review, mixed-park wake
# seam) - PostReleaseWake is returned, never acted on directly (the
# adapter must not call durably_mark_session_resumable itself - see that
# class's own docstring for the transactional-visibility hazard this
# split avoids).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_release_returns_none_when_sibling_still_outstanding() -> None:
    batch = ["b:tool:0:1", "b:tool:0:2"]
    releasing = _make_task("b:tool:0:1", batch_task_ids=batch)
    sibling_still_queued = _make_task(
        "b:tool:0:2", state=ToolCallTaskState.QUEUED, batch_task_ids=batch, claim_token=None,
    )
    storage = FakeStorage(releasing, sibling_still_queued)
    adapter = ToolCallClaimAdapter(task_storage=storage)

    result = await adapter.on_release(
        conn=None, entity_id="b:tool:0:1", outcome=_release("b:tool:0:1", success=True, drop_lease=True),
    )
    assert result is None
    # The releasing task's own row IS updated to DONE regardless - only
    # the WAKE is deferred, not this task's own terminal state.
    updated = await storage.get("b:tool:0:1")
    assert updated.state == ToolCallTaskState.DONE


@pytest.mark.asyncio
async def test_on_release_returns_wake_signal_when_last_sibling() -> None:
    batch = ["b:tool:0:1", "b:tool:0:2"]
    releasing = _make_task("b:tool:0:1", batch_task_ids=batch)
    sibling_already_done = _make_task(
        "b:tool:0:2", state=ToolCallTaskState.DONE, batch_task_ids=batch,
    )
    storage = FakeStorage(releasing, sibling_already_done)
    adapter = ToolCallClaimAdapter(task_storage=storage)

    result = await adapter.on_release(
        conn=None, entity_id="b:tool:0:1", outcome=_release("b:tool:0:1", success=True, drop_lease=True),
    )
    # event_key's node segment ("b") comes from the releasing task's own
    # scoped id ("b:tool:0:1") - see tool_wait_event_key's own docstring.
    assert result == PostReleaseWake(
        session_id="sess-1",
        event_key="tool_wait:sess-1:0:b",
        payload={"tool_wait_ready": True},
    )


@pytest.mark.asyncio
async def test_the_wake_key_comes_from_the_task_id_not_the_rows_turn_no() -> None:
    """The key is a pure function of the id: the turn segment is the one the id was minted with (``3``), whatever
    ``turn_no`` the row carries (mutation N27, call-site leg: key on ``task.turn_no``)."""
    batch = ["sess-1/a:b:tool:3:1", "sess-1/a:b:tool:3:2"]
    releasing = _make_task(batch[0], batch_task_ids=batch).model_copy(update={"turn_no": 7})
    sibling = _make_task(batch[1], state=ToolCallTaskState.DONE, batch_task_ids=batch).model_copy(
        update={"turn_no": 7},
    )
    adapter = ToolCallClaimAdapter(task_storage=FakeStorage(releasing, sibling))

    wake = await adapter.on_release(
        conn=None, entity_id=batch[0], outcome=_release(batch[0], success=True, drop_lease=True),
    )

    assert wake is not None and wake.event_key == "tool_wait:sess-1:3:a:b"


@pytest.mark.asyncio
async def test_a_malformed_id_on_the_last_sibling_commits_the_release_and_wakes_nothing(caplog) -> None:
    """The adapter computes the wake key INSIDE the fenced release transaction. A malformed id must not raise there:
    the raise would roll the release back and lose the task's result. The release lands (row DONE, lease dropped),
    no wake is returned, and the miss is logged at ERROR and counted (mutation N67: the adapter lets the error
    propagate)."""
    import primer.observability.metrics as metrics
    from primer.claim.in_memory import InMemoryClaimEngine

    metrics.reset_for_test()
    bad = "sess-1/x:tool:03:1"
    batch = [bad, "sess-1/x:tool:3:2"]
    storage = FakeStorage(
        _make_task(bad, batch_task_ids=batch),
        _make_task(batch[1], state=ToolCallTaskState.DONE, batch_task_ids=batch),
    )
    engine = InMemoryClaimEngine(adapters={ClaimKind.TOOL_CALL: ToolCallClaimAdapter(task_storage=storage)})
    woken: list[PostReleaseWake] = []

    async def _hook(signal: PostReleaseWake) -> None:
        woken.append(signal)

    engine.bind_post_release_hook(_hook)
    await engine.upsert(ClaimKind.TOOL_CALL, bad)
    [lease] = await engine.claim_due("worker-A", max_count=1)

    with caplog.at_level(logging.ERROR):
        await engine.release(lease, outcome=_release(bad, success=True, drop_lease=True))

    assert (await storage.get(bad)).state == ToolCallTaskState.DONE, "the release did not land"
    assert await engine.claim_due("worker-B", max_count=1) == [], "the lease was not dropped"
    assert woken == [], "a malformed id woke the session"
    assert metrics.tool_wait_malformed_scoped_id_total.labels("adapter")._value.get() == 1.0
    assert any(r.levelno == logging.ERROR and repr(bad) in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_on_release_wake_signal_treats_failed_as_terminal() -> None:
    """A sibling that FAILED (not just DONE) still counts as terminal for
    the purposes of "is the batch finished" - the batch is done either
    way, the resume coordinator is what decides how to represent a
    failed sibling's result."""
    batch = ["b:tool:0:1", "b:tool:0:2"]
    releasing = _make_task("b:tool:0:1", batch_task_ids=batch)
    sibling_failed = _make_task(
        "b:tool:0:2", state=ToolCallTaskState.FAILED, batch_task_ids=batch,
    )
    storage = FakeStorage(releasing, sibling_failed)
    adapter = ToolCallClaimAdapter(task_storage=storage)

    result = await adapter.on_release(
        conn=None, entity_id="b:tool:0:1",
        outcome=_release("b:tool:0:1", success=False, drop_lease=True, last_error="boom"),
    )
    assert result is not None
    assert result.session_id == "sess-1"


@pytest.mark.asyncio
async def test_on_release_reads_siblings_via_get_not_find() -> None:
    """The whole point of returning a signal instead of writing directly
    is a conn-scoped read - prove the sibling check goes through
    storage.get (which accepts conn) for every OTHER id in batch_task_ids,
    never a bulk find(). The releasing row itself comes back from the
    fenced write, so it is not read at all."""
    batch = ["b:tool:0:1", "b:tool:0:2", "b:tool:0:3"]
    releasing = _make_task("b:tool:0:1", batch_task_ids=batch)
    sibling_a = _make_task("b:tool:0:2", state=ToolCallTaskState.DONE, batch_task_ids=batch)
    sibling_b = _make_task("b:tool:0:3", state=ToolCallTaskState.DONE, batch_task_ids=batch)
    storage = FakeStorage(releasing, sibling_a, sibling_b)
    adapter = ToolCallClaimAdapter(task_storage=storage)

    await adapter.on_release(
        conn=None, entity_id="b:tool:0:1", outcome=_release("b:tool:0:1", success=True, drop_lease=True),
    )
    assert sorted(storage.get_calls) == ["b:tool:0:2", "b:tool:0:3"]
