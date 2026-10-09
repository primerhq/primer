"""A failed turn and the turn after it are two envelope runs, not one (ticket 01a11ce4).

``GET /v1/sessions/{sid}/turns/{n}/timeline`` joins window ``n`` to the ``n``-th RUN of turn-log envelopes. The turn log is grouped by ``turn_no``,
and a failed release does not bump ``turn_no`` (``SessionClaimAdapter.on_release`` bumps on success only), so the turn that follows a failed one
(a message to the failed session reopens it) writes its envelope under the SAME ``turn_no``: one group, one run, whose last event is the second
turn's ``completed``. The failed turn's trace then read "completed", and every later window was served another turn's envelope.

A new envelope opens at an own event (no ``node_id``) that is a ``resumed``, or a ``started`` that does not directly follow an own ``resumed``
(a resume writes ``resumed`` and then ``started`` for ONE envelope; a ``started`` after a turn that never wrote its end entry is a new turn). A run of
envelopes is one logical turn when a group's last own END event is ``yielded`` and the next group carries a later ``turn_no``. The last two tests drive
the production writers: turns that fail and succeed, each released as the pool releases it, each after the first started by a message to the session.
"""

from __future__ import annotations

import json

import pytest

from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.model.chat import Done, Error, TextDelta, TurnStreamFailure
from primer.model.workspace_session import WorkspaceSession
from primer.observability.turn_log_writer import TurnLogWriter
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn
from primer.session.enqueue import SessionWakeDeps, wake_session
from primer.session.timeline import build_turn_timeline, envelopes_for_window, turn_envelopes
from tests.session.test_dispatch import (  # noqa: F401  (fixtures are used by name)
    FakeExecutor,
    FakeWorkspaceIO,
    _make_lease,
    _seed_session,
    fake_event_bus,
    fake_storage_provider,
)


def _ev(seq, kind, turn_no, **extra):
    return json.dumps({"seq": seq, "kind": kind, "ts": "2026-10-08T00:00:00+00:00", "turn_no": turn_no, **extra})


def _kinds(group):
    return [e["kind"] for e in group]


def _times(group):
    """What the timeline reports for an envelope: its first `started` and its last end event (a `phase` can follow it)."""
    started = next(e["ts"] for e in group if e["kind"] == "started")
    ended = next(e["ts"] for e in reversed(group) if e["kind"] in {"completed", "failed", "cancelled"})
    return started, ended


def test_a_started_after_a_failed_on_the_same_turn_no_is_a_new_envelope():
    groups = turn_envelopes([
        _ev(1, "started", 3), _ev(2, "failed", 3, error={}),
        _ev(3, "started", 3), _ev(4, "completed", 3),
    ])

    assert [_kinds(g) for g in groups] == [["started", "failed"], ["started", "completed"]]
    assert _kinds(envelopes_for_window(groups, 0)[0]) == ["started", "failed"]
    assert _kinds(envelopes_for_window(groups, 1)[0]) == ["started", "completed"]


def test_a_started_after_a_cancelled_on_the_same_turn_no_is_a_new_envelope():
    groups = turn_envelopes([
        _ev(1, "started", 3), _ev(2, "cancelled", 3),
        _ev(3, "started", 3), _ev(4, "completed", 3),
    ])

    assert [_kinds(g) for g in groups] == [["started", "cancelled"], ["started", "completed"]]


def test_a_turn_whose_end_entry_never_landed_is_split_from_the_next():
    """A worker crash or a lost lease writes the release marker but no turn-log `failed`: the next `started` is the only boundary."""
    groups = turn_envelopes([
        _ev(1, "started", 3), _ev(2, "phase", 3),
        _ev(3, "started", 3), _ev(4, "completed", 3),
    ])

    assert [_kinds(g) for g in groups] == [["started", "phase"], ["started", "completed"]]
    assert [len(envelopes_for_window(groups, i)) for i in range(3)] == [1, 1, 0]


def test_a_late_phase_event_stays_with_the_envelope_it_belongs_to():
    groups = turn_envelopes([
        _ev(1, "started", 3), _ev(2, "failed", 3, error={}), _ev(3, "phase", 3),
        _ev(4, "started", 3), _ev(5, "completed", 3),
    ])

    assert [_kinds(g) for g in groups] == [["started", "failed", "phase"], ["started", "completed"]]


def test_a_resume_is_one_envelope_not_two():
    """A resume writes `resumed` and then `started`: the `started` that directly follows a `resumed` opens nothing."""
    groups = turn_envelopes([
        _ev(1, "started", 4), _ev(2, "yielded", 4, yield_kind="ask_user", event_key="k"),
        _ev(3, "resumed", 4, wait_ms=5, resume_kind="event_fired"), _ev(4, "started", 4), _ev(5, "completed", 4),
    ])

    assert [_kinds(g) for g in groups] == [["started", "yielded"], ["resumed", "started", "completed"]]


def test_a_park_and_its_continuation_under_the_next_turn_no_are_one_run_even_with_phase_events_after_the_yield():
    """`phase('waiting')` follows a `yielded` (dispatch.py), so the park's last event is not its end: the run is decided by the last END event."""
    groups = turn_envelopes([
        _ev(1, "started", 3), _ev(2, "phase", 3), _ev(3, "yielded", 3, yield_kind="ask_user", event_key="k"), _ev(4, "phase", 3),
        _ev(5, "resumed", 4, wait_ms=5, resume_kind="event_fired"), _ev(6, "started", 4), _ev(7, "phase", 4), _ev(8, "completed", 4),
        _ev(9, "started", 5), _ev(10, "completed", 5),
    ])

    run0 = envelopes_for_window(groups, 0)
    assert [_kinds(g) for g in run0] == [["started", "phase", "yielded", "phase"], ["resumed", "started", "phase", "completed"]]
    assert _kinds(envelopes_for_window(groups, 1)[0]) == ["started", "completed"]


def test_a_park_and_its_continuation_without_a_resumed_are_one_run():
    """The production shape of a continuation: `started` alone under a later `turn_no` (`resumed` is written only when `parked_at` is set)."""
    groups = turn_envelopes([
        _ev(1, "started", 3), _ev(2, "phase", 3), _ev(3, "yielded", 3, yield_kind="ask_user", event_key="k"), _ev(4, "phase", 3),
        _ev(5, "started", 4), _ev(6, "phase", 4), _ev(7, "completed", 4),
        _ev(8, "started", 5), _ev(9, "completed", 5),
    ])

    run0 = envelopes_for_window(groups, 0)
    assert [_kinds(g) for g in run0] == [["started", "phase", "yielded", "phase"], ["started", "phase", "completed"]]
    assert _kinds(envelopes_for_window(groups, 1)[0]) == ["started", "completed"]


def test_a_yielded_then_resumed_on_one_turn_no_is_two_runs():
    """`abandon_session_gate` clears the park but leaves `parked_at` set, and writes no turn-log event and no release: the next turn writes
    `resumed` (then `started`) on the `turn_no` the park never bumped. That continues the session as a NEW turn: two turns, two runs."""
    groups = turn_envelopes([
        _ev(1, "started", 3), _ev(2, "yielded", 3, yield_kind="ask_user", event_key="k"),
        _ev(3, "resumed", 3, wait_ms=5, resume_kind="event_fired"), _ev(4, "started", 3), _ev(5, "completed", 3),
    ])

    assert [len(envelopes_for_window(groups, i)) for i in range(3)] == [1, 1, 0]
    assert _kinds(envelopes_for_window(groups, 0)[0]) == ["started", "yielded"]
    assert _kinds(envelopes_for_window(groups, 1)[0]) == ["resumed", "started", "completed"]


def test_a_graph_nodes_events_do_not_split_an_envelope():
    groups = turn_envelopes([
        _ev(1, "started", 3), _ev(2, "started", 3, node_id="n1"), _ev(3, "completed", 3, node_id="n1"),
        _ev(4, "started", 3, node_id="n2"), _ev(5, "completed", 3),
    ])

    assert len(groups) == 1


def test_a_graph_nodes_events_between_two_turns_stay_with_the_turn_they_follow():
    groups = turn_envelopes([
        _ev(1, "started", 3), _ev(2, "failed", 3, error={}), _ev(3, "started", 3, node_id="n1"), _ev(4, "completed", 3, node_id="n1"),
        _ev(5, "started", 3), _ev(6, "completed", 3),
    ])

    assert [_kinds(g) for g in groups] == [["started", "failed", "started", "completed"], ["started", "completed"]]


# ---- the production writers -------------------------------------------------------------------------------------------------------------------


class _TurnLog(TurnLogWriter):
    """One turns.jsonl for the whole test: what the workspace file would hold across the turns."""

    def __init__(self) -> None:
        self.events: list = []

    async def append(self, event):
        seq = len(self.events) + 1
        self.events.append(event.model_copy(update={"seq": seq}))
        return seq

    async def aclose(self):
        return None


class _Slot:
    async def reopen(self) -> None: ...

    async def append_instruction(self, content, *, extra_parts=None) -> None: ...


class _Workspace(FakeWorkspaceIO):
    async def get_session(self, session_id):
        return _Slot()


class _Registry:
    def __init__(self, workspace) -> None:
        self._workspace = workspace

    async def get_workspace(self, workspace_id):
        return self._workspace

    async def get_workspace_row(self, workspace_id):
        return None


class _Scheduler:
    async def enqueue(self, session_id) -> None: ...


class _Engine:
    async def upsert(self, *args, **kwargs) -> None: ...


async def _play(fake_storage_provider, fake_event_bus, plan: list[str]):
    """Run the turns of ``plan`` ("fail" or "ok") through the production writers, one message per turn after the first.

    Every turn is followed by the claim adapter's release as the pool makes it (a success bumps ``turn_no``, a failure writes the marker); every
    turn after the first is started by a message to the session (``wake_session``), which reopens it when the last one failed. One turn-log
    writer serves all of them, as the file does.
    """
    sid = "s-trace"
    workspace, turn_log = _Workspace(), _TurnLog()
    await _seed_session(fake_storage_provider, sid)
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    row = await sessions.get(sid)
    await sessions.update(row.model_copy(update={"turn_no": 3, "completed_turn_no": 2}))
    adapter = SessionClaimAdapter(session_storage=sessions, workspace_registry=_Registry(workspace), event_bus=fake_event_bus)
    wake = SessionWakeDeps(
        storage_provider=fake_storage_provider, scheduler=_Scheduler(), claim_engine=_Engine(),
        workspace_registry=_Registry(workspace), event_bus=fake_event_bus,
    )

    def deps_for(executor):
        async def build(_session):
            return executor

        return SessionDispatchDeps(
            storage_provider=fake_storage_provider, workspace_io=workspace, event_bus=fake_event_bus,
            build_executor=build, turn_log_writer_factory=lambda _io, _sid: turn_log,
        )

    outcomes = []
    for n, kind in enumerate(plan):
        if n:
            await wake_session(
                workspace_id=row.workspace_id, session_id=sid, instruction=f"message {n}", human_intent=True, deps=wake,
            )
        if kind == "fail":
            executor = FakeExecutor([TextDelta(text="hi", index=0), RuntimeError("boom")])
        elif kind.startswith("stream_fail:"):
            # a fatal stream Error, then the failure the loop raises when the stream ends
            error = Error(code="server_error", message=kind.split(":", 1)[1], fatal=True)
            executor = FakeExecutor([TextDelta(text="hi", index=0), error, TurnStreamFailure(error, partial_messages=[], rounds_completed=0)])
        else:
            executor = FakeExecutor([TextDelta(text="done", index=0), Done(stop_reason="stop", raw_reason="stop")])
        outcome = await run_one_session_turn(_make_lease(sid), deps_for(executor))
        assert outcome.success is (kind == "ok")
        await adapter.on_release(None, sid, outcome=outcome)         # the pool's release: bumps turn_no on success, writes the marker on failure
        outcomes.append(outcome)
    return workspace.read_lines(sid), [event.model_dump_json() for event in turn_log.events], turn_log


@pytest.mark.asyncio
async def test_the_timeline_of_a_failed_turn_is_its_own_envelope_not_the_next_turns(fake_storage_provider, fake_event_bus):
    message_lines, turn_log_lines, turn_log = await _play(fake_storage_provider, fake_event_bus, ["fail", "ok"])

    ends = [e.kind.value for e in turn_log.events if e.kind.value in {"failed", "completed"}]
    assert ends == ["failed", "completed"], ends
    assert len({e.turn_no for e in turn_log.events if e.kind.value in {"started", "failed", "completed"}}) == 1, (
        "premise: both turns wrote their envelope under one turn_no"
    )
    first_turn = build_turn_timeline(message_lines=message_lines, turn_log_lines=turn_log_lines, turn_no=0)
    assert first_turn is not None
    assert first_turn["status"] == "failed", "the failed turn's trace reads as the turn after it"
    runs = [envelopes_for_window(turn_envelopes(turn_log_lines), i) for i in range(3)]
    assert [len(run) for run in runs] == [1, 1, 0], "one run per turn: the failed one, the one that followed, and no third"
