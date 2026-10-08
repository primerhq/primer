"""A failed turn and the turn after it are two envelope runs, not one (ticket 01a11ce4).

``GET /v1/sessions/{sid}/turns/{n}/timeline`` joins window ``n`` to the ``n``-th RUN of turn-log envelopes. The turn log is grouped by ``turn_no``,
and a failed release does not bump ``turn_no`` (``SessionClaimAdapter.on_release`` bumps on success only), so the turn that follows a failed one
(a message to the failed session reopens it) writes its envelope under the SAME ``turn_no``: one group, one run, whose last event is the second
turn's ``completed``. The failed turn's trace then read "completed", and every later window was served another turn's envelope.

A ``turn_no`` that has already ended (completed, failed, cancelled, yielded) and then restarts (``started``, or the ``resumed`` that precedes it)
is a new envelope. The last test drives the production writers: a turn that fails, its release, a message that reopens the session, the next turn.
"""

from __future__ import annotations

import json

import pytest

from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.model.chat import Done, TextDelta
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


def test_a_started_after_a_failed_on_the_same_turn_no_is_a_new_envelope():
    groups = turn_envelopes([
        _ev(1, "started", 3), _ev(2, "failed", 3, error={}),
        _ev(3, "started", 3), _ev(4, "completed", 3),
    ])

    assert [_kinds(g) for g in groups] == [["started", "failed"], ["started", "completed"]]
    assert _kinds(envelopes_for_window(groups, 0)[0]) == ["started", "failed"]
    assert _kinds(envelopes_for_window(groups, 1)[0]) == ["started", "completed"]


def test_a_late_phase_event_stays_with_the_envelope_it_belongs_to():
    groups = turn_envelopes([
        _ev(1, "started", 3), _ev(2, "failed", 3, error={}), _ev(3, "phase", 3),
        _ev(4, "started", 3), _ev(5, "completed", 3),
    ])

    assert [_kinds(g) for g in groups] == [["started", "failed", "phase"], ["started", "completed"]]


def test_a_parked_turn_resumed_on_the_same_turn_no_is_still_one_run():
    """resumed + started after a yielded: two groups now, but the yielded one is continued by the next, so ONE run as before."""
    groups = turn_envelopes([
        _ev(1, "started", 3), _ev(2, "yielded", 3, yield_kind="ask_user", event_key="k"),
        _ev(3, "resumed", 3, wait_ms=5, resume_kind="event_fired"), _ev(4, "started", 3), _ev(5, "completed", 3),
        _ev(6, "started", 4), _ev(7, "completed", 4),
    ])

    run0 = envelopes_for_window(groups, 0)
    assert [_kinds(g) for g in run0] == [["started", "yielded"], ["resumed", "started", "completed"]]
    assert _kinds(envelopes_for_window(groups, 1)[0]) == ["started", "completed"]


def test_a_graph_nodes_events_do_not_split_an_envelope():
    groups = turn_envelopes([
        _ev(1, "started", 3), _ev(2, "started", 3, node_id="n1"), _ev(3, "completed", 3, node_id="n1"),
        _ev(4, "started", 3, node_id="n2"), _ev(5, "completed", 3),
    ])

    assert len(groups) == 1


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


@pytest.mark.asyncio
async def test_the_timeline_of_a_failed_turn_is_its_own_envelope_not_the_next_turns(fake_storage_provider, fake_event_bus):
    sid = "s-trace"
    workspace, turn_log = _Workspace(), _TurnLog()
    await _seed_session(fake_storage_provider, sid)
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    row = await sessions.get(sid)
    await sessions.update(row.model_copy(update={"turn_no": 3, "completed_turn_no": 2}))

    def deps_for(executor):
        async def build(_session):
            return executor

        return SessionDispatchDeps(
            storage_provider=fake_storage_provider, workspace_io=workspace, event_bus=fake_event_bus,
            build_executor=build, turn_log_writer_factory=lambda _io, _sid: turn_log,
        )

    # the turn fails; its release (success=False) does not bump turn_no and writes the claim adapter's marker
    failed = await run_one_session_turn(_make_lease(sid), deps_for(FakeExecutor([TextDelta(text="hi", index=0), RuntimeError("boom")])))
    assert failed.success is False
    adapter = SessionClaimAdapter(session_storage=sessions, workspace_registry=_Registry(workspace), event_bus=fake_event_bus)
    await adapter.on_release(None, sid, outcome=failed)
    # a message to the failed session reopens it; the next turn runs under the same turn_no and completes
    await wake_session(
        workspace_id=row.workspace_id, session_id=sid, instruction="try again", human_intent=True,
        deps=SessionWakeDeps(
            storage_provider=fake_storage_provider, scheduler=_Scheduler(), claim_engine=_Engine(),
            workspace_registry=_Registry(workspace), event_bus=fake_event_bus,
        ),
    )
    second = await run_one_session_turn(
        _make_lease(sid), deps_for(FakeExecutor([TextDelta(text="done", index=0), Done(stop_reason="stop", raw_reason="stop")])),
    )
    assert second.success is True

    message_lines = workspace.read_lines(sid)
    turn_log_lines = [event.model_dump_json() for event in turn_log.events]
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
