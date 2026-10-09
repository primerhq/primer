"""A graph turn is ONE closed window per invocation, with the records the REAL writers produce (ticket 01a11f35, round 2 of #701).

Round 1 of #701 made a record with a ``node_id`` INSIDE its window and left the window to be closed by "the graph's own end", a node-less terminal. Production wrote none, so a graph turn never
closed (``usage.turns`` 0, a second invocation merged into window 0, ``/turns/1`` a 404): the measurement came from a harness that APPENDED a fabricated ``Done``. Nothing in this module is
fabricated. It drives the real ``run_one_session_turn`` with a real ``WorkspaceGraphExecutor`` (a two-worker fan-out), the second invocation opened the way ``wake_session``'s reopen opens it, and
reads what the writers left in ``messages.jsonl`` through the shipped readers: ``turn_windows``, ``session_usage``, ``derive_session_final_text`` and ``build_turn_timeline``. The hand-built corpus
(``tests/session/graph_turn_shapes.py``, ``tests/ui/test_shell_turns.py``) stays for the scanner's unit cases and says so.

The one thing a harness has to supply is what the claim adapter's success release does after a turn (it bumps ``turn_no``); ``_open`` does it for a reopened invocation.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from primer.channel.session_relay import derive_session_final_text
from primer.model.chat import Done, Error, TextDelta
from primer.model.graph import FanOutSpec, Graph, _AgentNodeRef, _BeginNode, _EndNode, _FanInNode, _FanOutNode, _StaticEdge
from primer.model.workspace_session import GraphSessionBinding, SessionMessageKind, SessionMessageRecord, SessionStatus, WorkspaceSession
from primer.session.dispatch import run_one_session_turn
from primer.session.persistence import WorkspaceMessageWriter
from primer.session.timeline import build_turn_timeline, turn_windows
from primer.session.turns import has_open_turn
from primer.session.usage import session_usage
from tests.graph.test_workspace_executor import _agent, _build_executor, _FakeLLM, _make_state_repo
from tests.session.test_dispatch import (  # noqa: F401  (fixtures)
    FakeWorkspaceIO, _make_lease, _now, fake_event_bus, fake_storage_provider, fake_workspace_io,
)
from tests.session.test_dispatch_interrupt import _deps

SID = "gs-real"

OK = [[TextDelta(text="worker done", index=0), Done(stop_reason="stop", raw_reason="stop")]]
FAIL = [[TextDelta(text="half", index=0), Error(message="boom", code="server_error", fatal=True)]]


def _fanout(on_failure: str = "fail_fast") -> Graph:
    return Graph.model_construct(
        id="g-real", description="fan-out",
        nodes=[
            _BeginNode(id="begin"),
            _FanOutNode(id="fan", specs=[FanOutSpec(kind="broadcast", target_node_id="worker", count=2, on_failure=on_failure)]),
            _AgentNodeRef(id="worker", agent_id="x", input_template="W{{ fanout_index }}"),
            _FanInNode(id="agg", aggregate_template="{% for n in nodes.worker %}{{ n.text }}{% endfor %}"),
            _EndNode(id="end", output_template="{{ nodes.agg.text }}"),
        ],
        edges=[_StaticEdge(from_node="begin", to_node="fan"), _StaticEdge(from_node="worker", to_node="agg"), _StaticEdge(from_node="agg", to_node="end")],
        max_iterations=10, harness_id=None,
    )


async def _open(storage, io, *, reopen: bool = False):
    """Write (a reopen's invocation_divider, then) a user_input, as create_session / wake_session do; return the row."""
    sessions = storage.get_storage(WorkspaceSession)
    row = await sessions.get(SID)
    if row is None:
        row = WorkspaceSession(id=SID, workspace_id="w1", binding=GraphSessionBinding(graph_id="g-real"), status=SessionStatus.RUNNING, created_at=_now(), turn_status="running")
        await sessions.create(row)
        row = await sessions.get(SID)
    writer = WorkspaceMessageWriter(workspace_io=io, session_id=SID, start_seq=row.last_seq)
    if reopen:
        await writer.append(SessionMessageRecord(seq=1, kind=SessionMessageKind.INVOCATION_DIVIDER, payload={"invocation": 2}, created_at=datetime.now(timezone.utc)))
    last = await writer.append(SessionMessageRecord(seq=1, kind=SessionMessageKind.USER_INPUT, payload={"text": "go"}, created_at=datetime.now(timezone.utc)))
    await writer.flush()
    row = await sessions.get(SID)
    bump = {"turn_no": row.turn_no + 1} if reopen else {}       # the claim adapter's success release bumps turn_no; this harness does not run it
    row = row.model_copy(update={"last_seq": last, "status": SessionStatus.RUNNING, "ended_reason": None, "ended_at": None, "turn_status": "running", **bump})
    await sessions.update(row)
    return row


async def _run(tmp_path, storage, io, bus, scripts, on_failure: str = "fail_fast", graph: Graph | None = None):
    repo = await _make_state_repo(tmp_path)
    executor = await _build_executor(graph=graph or _fanout(on_failure), llm=_FakeLLM(scripts=scripts), state_repo=repo, graph_session_id=SID, agents={"x": _agent("x")})
    outcome = await run_one_session_turn(_make_lease(SID), _deps(storage, io, bus, executor))
    return outcome, await storage.get_storage(WorkspaceSession).get(SID)


def _records(io) -> list[dict]:
    return [json.loads(line) for line in io.read_lines(SID)]


def _one_closed_window(lines: list[str], *, of: int = 0) -> dict:
    windows = turn_windows(lines)
    assert [w["turn_no"] for w in windows] == list(range(len(windows)))
    window = windows[of]
    assert window["terminal_seq"] is not None, f"window {of} never closed: {[(w['turn_no'], w['terminal_seq']) for w in windows]}"
    return window


def _terminal_of(records: list[dict], seq: int) -> dict:
    return next(r for r in records if r["seq"] == seq)


# ---- a graph that succeeds ---------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_fanout_that_succeeds_is_one_closed_window_ended_by_the_graphs_own_end(tmp_path, fake_workspace_io, fake_event_bus, fake_storage_provider) -> None:
    await _open(fake_storage_provider, fake_workspace_io)

    outcome, row = await _run(tmp_path, fake_storage_provider, fake_workspace_io, fake_event_bus, OK)

    lines, records = fake_workspace_io.read_lines(SID), _records(fake_workspace_io)
    assert outcome.success and row.ended_reason == "completed"
    assert len(turn_windows(lines)) == 1, "a fan-out of two workers is one window, not one per finished node"
    window = _one_closed_window(lines)
    end = _terminal_of(records, window["terminal_seq"])
    assert (end["kind"], end.get("node_id")) == ("done", None), end            # the graph's own end: a node-less done, the last record written
    assert end["seq"] == records[-1]["seq"], "the end is the last record of the turn"
    assert (end["payload"] or {}).get("stop_reason") == "stop"
    assert session_usage(lines).turns == 1
    assert not has_open_turn(lines, cursor=row.next_unprocessed_seq)


@pytest.mark.asyncio
async def test_the_graphs_end_is_not_a_model_call(tmp_path, fake_workspace_io, fake_event_bus, fake_storage_provider) -> None:
    await _open(fake_storage_provider, fake_workspace_io)
    await _run(tmp_path, fake_storage_provider, fake_workspace_io, fake_event_bus, OK)

    usage = session_usage(fake_workspace_io.read_lines(SID))

    assert usage.model_calls == 2, "the two workers' answers are the model calls; the end of the graph is not one"


@pytest.mark.asyncio
async def test_the_relay_still_posts_the_end_nodes_text(tmp_path, fake_workspace_io, fake_event_bus, fake_storage_provider) -> None:
    """The End output is written BEFORE the graph's own end: the final-result relay must still find it."""
    await _open(fake_storage_provider, fake_workspace_io)
    await _run(tmp_path, fake_storage_provider, fake_workspace_io, fake_event_bus, OK)

    assert derive_session_final_text(_records(fake_workspace_io)) == "worker doneworker done"


@pytest.mark.asyncio
async def test_the_timeline_of_the_turn_is_completed_and_names_the_end(tmp_path, fake_workspace_io, fake_event_bus, fake_storage_provider) -> None:
    await _open(fake_storage_provider, fake_workspace_io)
    await _run(tmp_path, fake_storage_provider, fake_workspace_io, fake_event_bus, OK)
    lines = fake_workspace_io.read_lines(SID)

    timeline = build_turn_timeline(message_lines=lines, turn_log_lines=[], turn_no=0)

    assert timeline is not None and timeline["terminal_seq"] == _one_closed_window(lines)["terminal_seq"]
    assert timeline["status"] != "running", "with no turn log the status is read off the window's last record, which is the graph's end"


# ---- a graph that fails ------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_fanout_whose_workers_fail_fast_is_one_closed_failed_window(tmp_path, fake_workspace_io, fake_event_bus, fake_storage_provider) -> None:
    await _open(fake_storage_provider, fake_workspace_io)

    outcome, row = await _run(tmp_path, fake_storage_provider, fake_workspace_io, fake_event_bus, FAIL)

    lines, records = fake_workspace_io.read_lines(SID), _records(fake_workspace_io)
    assert row.ended_reason == "failed"
    assert len(turn_windows(lines)) == 1
    window = _one_closed_window(lines)
    end = _terminal_of(records, window["terminal_seq"])
    assert end.get("node_id") is None and end["seq"] == records[-1]["seq"], end
    assert (end["kind"], (end["payload"] or {}).get("stop_reason")) == ("done", "error"), "a failure end: what every failed turn's verdict reads"
    assert session_usage(lines).turns == 1
    assert derive_session_final_text(records) is None, "a failed graph relays no result"
    timeline = build_turn_timeline(message_lines=lines, turn_log_lines=[], turn_no=0)
    assert timeline is not None and timeline["status"] == "failed"


@pytest.mark.asyncio
async def test_a_fanout_that_collects_its_failures_is_one_closed_window_ended_as_the_row_is(tmp_path, fake_workspace_io, fake_event_bus, fake_storage_provider) -> None:
    """``on_failure="collect"``: one worker fails, the other answers, the graph runs on to its End. The end record says what the row says."""
    await _open(fake_storage_provider, fake_workspace_io)

    outcome, row = await _run(tmp_path, fake_storage_provider, fake_workspace_io, fake_event_bus, [FAIL[0], OK[0]], on_failure="collect")

    lines, records = fake_workspace_io.read_lines(SID), _records(fake_workspace_io)
    assert len(turn_windows(lines)) == 1
    window = _one_closed_window(lines)
    end = _terminal_of(records, window["terminal_seq"])
    assert end.get("node_id") is None and end["seq"] == records[-1]["seq"], end
    expected = "stop" if row.ended_reason == "completed" else "error"
    assert (end["kind"], (end["payload"] or {}).get("stop_reason")) == ("done", expected), (row.ended_reason, end)
    assert session_usage(lines).turns == 1


@pytest.mark.asyncio
async def test_a_graph_that_runs_out_of_iterations_is_one_closed_failed_window(tmp_path, fake_workspace_io, fake_event_bus, fake_storage_provider) -> None:
    """A graph-LEVEL failure names no node: the executor's ``max_iterations_exceeded`` error is a node-less terminal and closes the window; the graph's own end that dispatch appends after it
    is a copy of that failure, not a second window."""
    await _open(fake_storage_provider, fake_workspace_io)
    capped = _one_worker().model_copy(update={"max_iterations": 1})

    outcome, row = await _run(tmp_path, fake_storage_provider, fake_workspace_io, fake_event_bus, OK, graph=capped)

    lines, records = fake_workspace_io.read_lines(SID), _records(fake_workspace_io)
    assert row.ended_reason == "failed"
    graph_error = next(r for r in records if r["kind"] == "error" and not r.get("node_id"))
    assert (graph_error["payload"] or {}).get("code") == "max_iterations_exceeded", [(r["kind"], r.get("node_id")) for r in records]
    assert len(turn_windows(lines)) == 1, "the graph-level error and the graph's end are one failure"
    window = _one_closed_window(lines)
    assert window["terminal_seq"] == graph_error["seq"] and window["records"][-1]["seq"] == records[-1]["seq"], "the end is filed in the window it copies"
    assert (records[-1]["kind"], (records[-1]["payload"] or {}).get("stop_reason")) == ("done", "error")
    assert session_usage(lines).turns == 1
    assert derive_session_final_text(records) is None


# ---- two invocations of one session (the reopen path) --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_invocations_are_two_closed_windows_and_the_second_envelope_resolves(tmp_path, fake_workspace_io, fake_event_bus, fake_storage_provider) -> None:
    await _open(fake_storage_provider, fake_workspace_io)
    await _run(tmp_path / "a", fake_storage_provider, fake_workspace_io, fake_event_bus, OK)
    await _open(fake_storage_provider, fake_workspace_io, reopen=True)
    await _run(tmp_path / "b", fake_storage_provider, fake_workspace_io, fake_event_bus, OK)

    lines, records = fake_workspace_io.read_lines(SID), _records(fake_workspace_io)

    windows = turn_windows(lines)
    assert [w["turn_no"] for w in windows] == [0, 1], "one window per invocation (it merged the second into window 0)"
    first, second = _one_closed_window(lines, of=0), _one_closed_window(lines, of=1)
    assert first["terminal_seq"] < second["terminal_seq"]
    assert all(r["seq"] <= first["terminal_seq"] for r in first["records"]) and all(r["seq"] > first["terminal_seq"] for r in second["records"])
    assert [r["kind"] for r in second["records"]][:2] == ["invocation_divider", "user_input"], "the reopen's records open the second window"
    assert session_usage(lines).turns == 2
    assert not has_open_turn(lines, cursor=records[-1]["seq"] + 1)
    # The turn log is the one thing here that is not what the writers left: two envelopes, to pair the windows with.
    turn_log = [json.dumps({"seq": i + 1, "kind": kind, "turn_no": n, "ts": f"2026-10-09T12:0{n}:0{i}+00:00"}) for i, (kind, n) in enumerate([("started", 0), ("completed", 0), ("started", 1), ("completed", 1)])]
    timeline = build_turn_timeline(message_lines=lines, turn_log_lines=turn_log, turn_no=1)
    assert timeline is not None and timeline["terminal_seq"] == second["terminal_seq"], "/turns/1 is the second invocation"


# ---- a graph that parks and is resumed ---------------------------------------------------------------------------------------------------------------


def _one_worker() -> Graph:
    return Graph.model_construct(
        id="g-real", description="one worker",
        nodes=[_BeginNode(id="begin"), _AgentNodeRef(id="worker", agent_id="x", input_template="go"), _EndNode(id="end", output_template="{{ nodes.worker.text }}")],
        edges=[_StaticEdge(from_node="begin", to_node="worker"), _StaticEdge(from_node="worker", to_node="end")],
        max_iterations=10, harness_id=None,
    )


@pytest.mark.asyncio
async def test_a_graph_that_parks_and_is_resumed_is_one_closed_window(tmp_path, monkeypatch, fake_workspace_io, fake_event_bus, fake_storage_provider) -> None:
    """The park through the real ``run_one_session_turn`` (it writes no terminal: the turn is not over), the resume through the real ``resume_graph_engine``, which ends the graph through the pool."""
    from datetime import UTC

    from primer.model.yield_ import Yielded, YieldToWorker
    from primer.worker import graph_resume_coordinator
    from primer.worker.yield_runtime import ParkedState
    from tests._resume_hook_fakes import EngineFakePool
    from tests.graph.test_tool_wait_graph_park import _patch_run_agent_turn

    ask = YieldToWorker(
        Yielded(tool_name="ask_user", event_key=f"ask_user:{SID}:worker:tc-1", resume_metadata={"prompt": "color?"}),
        tool_call_id="tc-1",
        llm_messages=[{"role": "assistant", "parts": [{"type": "text", "text": "worker asking"}]}],
    )
    _patch_run_agent_turn(monkeypatch, {"x": ask})
    await _open(fake_storage_provider, fake_workspace_io)
    repo = await _make_state_repo(tmp_path)

    async def executor():
        return await _build_executor(graph=_one_worker(), llm=_FakeLLM(scripts=OK), state_repo=repo, graph_session_id=SID, agents={"x": _agent("x")})

    parked_outcome = await run_one_session_turn(_make_lease(SID), _deps(fake_storage_provider, fake_workspace_io, fake_event_bus, await executor()))
    parked_row = await fake_storage_provider.get_storage(WorkspaceSession).get(SID)
    assert parked_outcome.drop_lease and parked_outcome.park is not None, parked_outcome          # the claim adapter's release writes the WAITING row from this; the harness does not run it
    assert all(r["kind"] not in ("done", "cancelled") or r.get("node_id") for r in _records(fake_workspace_io)), "a parked turn is not over: nothing closes it"
    assert turn_windows(fake_workspace_io.read_lines(SID))[0]["terminal_seq"] is None

    blob = {**parked_outcome.park.parked_state, "resume_event_payload": {"response": "blue"}}
    session = parked_row.model_copy(update={
        "status": SessionStatus.WAITING, "parked_state": {"resume_event_key": ask.yielded.event_key}, "parked_at": datetime.now(UTC),
    })
    pool = EngineFakePool(storage=fake_storage_provider, workspace_io=fake_workspace_io, executor_factory=executor)
    resumed = await graph_resume_coordinator.resume_graph_engine(pool, session, ParkedState.from_jsonable(blob))

    lines = fake_workspace_io.read_lines(SID)
    assert resumed == "ENDED:completed" and pool.end_session_calls == ["completed"], (resumed, pool.end_session_calls)
    assert len(turn_windows(lines)) == 1, "the park and the resume are one turn"
    window = _one_closed_window(lines)
    end = _terminal_of(_records(fake_workspace_io), window["terminal_seq"])
    assert (end["kind"], end.get("node_id"), (end["payload"] or {}).get("stop_reason")) == ("done", None, "stop"), end
    assert end["seq"] == _records(fake_workspace_io)[-1]["seq"]
    assert session_usage(lines).turns == 1
