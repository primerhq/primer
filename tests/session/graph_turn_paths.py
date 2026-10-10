"""The paths a graph session's log can take, produced by the REAL writers (ticket 01a11f35, round 3 of #701). A helper module for the tests that read those logs, not a test.

Everything here drives ``run_one_session_turn`` with a real ``WorkspaceGraphExecutor`` (and, for the park, a real park through dispatch), writes ``turns.jsonl`` through the real ``WorkspaceTurnLogWriter`` and
opens each invocation the way ``create_session`` / ``wake_session`` / ``restart_session`` do (a reopen is an ``invocation_divider``, then a ``user_input`` unless the session was restarted with no input). What the harness
still supplies is what the claim adapter does around a turn: the success release bumps ``turn_no`` (``release``), a failed run gets its release marker (``run_raising``), and a park sets the park columns.

``LegacyWriters`` switches the graph's end writers off: the log of a graph run as it was written before the end was a record (round 2 of this ticket), so the readers' treatment of those logs is pinned with the logs
the old code really wrote, not hand-made ones.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timezone

from primer.model.chat import Done, Error, TextDelta
from primer.model.graph import FanOutSpec, Graph, _AgentNodeRef, _BeginNode, _EndNode, _FanInNode, _FanOutNode, _StaticEdge
from primer.model.workspace_session import GraphSessionBinding, SessionMessageKind, SessionMessageRecord, SessionStatus, WorkspaceSession
from primer.observability.turn_log_writer import WorkspaceTurnLogWriter
from primer.session import dispatch as dispatch_mod
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn
from primer.session.persistence import WorkspaceMessageWriter
from primer.worker import graph_resume_coordinator
from tests.graph.test_workspace_executor import _agent, _build_executor, _FakeLLM, _make_state_repo
from tests.session.graph_turn_shapes import release_marker
from tests.session.test_dispatch import _make_lease, _now

SID = "gs-paths"
OK = [TextDelta(text="worker done", index=0), Done(stop_reason="stop", raw_reason="stop")]
FAIL = [TextDelta(text="half", index=0), Error(message="boom", code="server_error", fatal=True)]


class TurnLogs:
    """A ``turns.jsonl`` per session, written by the REAL ``WorkspaceTurnLogWriter`` that dispatch builds through ``turn_log_writer_factory``."""

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    def factory(self, workspace_io, session_id):
        async def append(line):
            self.data[session_id] = self.data.get(session_id, b"") + (line if isinstance(line, bytes) else line.encode())

        async def read():
            return self.data.get(session_id, b"")

        return WorkspaceTurnLogWriter(append_line=append, read_existing=read)

    def lines(self, sid: str = SID) -> list[str]:
        return [ln for ln in self.data.get(sid, b"").decode().splitlines() if ln.strip()]

    def own_started(self) -> list[str]:
        """The ``ts`` of every turn envelope's own ``started`` event (not a node's), in order: the n-th is what ``/turns/n`` must report as its start."""
        events = [json.loads(ln) for ln in self.lines()]
        return [e["ts"] for e in events if e.get("kind") == "started" and not e.get("node_id")]


def deps(storage, io, bus, executor, tl: TurnLogs) -> SessionDispatchDeps:
    async def build(_session):
        return executor

    return SessionDispatchDeps(storage_provider=storage, workspace_io=io, event_bus=bus, build_executor=build, turn_log_writer_factory=tl.factory)


def fanout(on_failure: str = "fail_fast", *, end_template: str | None = "{{ nodes.agg.text }}", max_iterations: int = 10) -> Graph:
    end = _EndNode(id="end", output_template=end_template) if end_template is not None else _EndNode(id="end")
    return Graph.model_construct(
        id="g-paths", description="fan-out",
        nodes=[
            _BeginNode(id="begin"),
            _FanOutNode(id="fan", specs=[FanOutSpec(kind="broadcast", target_node_id="worker", count=2, on_failure=on_failure)]),
            _AgentNodeRef(id="worker", agent_id="x", input_template="W{{ fanout_index }}"),
            _FanInNode(id="agg", aggregate_template="{% for n in nodes.worker %}{{ n.text }}{% endfor %}"),
            end,
        ],
        edges=[_StaticEdge(from_node="begin", to_node="fan"), _StaticEdge(from_node="worker", to_node="agg"), _StaticEdge(from_node="agg", to_node="end")],
        max_iterations=max_iterations, harness_id=None,
    )


def one_worker(*, end_template: str | None = "{{ nodes.worker.text }}", max_iterations: int = 10) -> Graph:
    end = _EndNode(id="end", output_template=end_template) if end_template is not None else _EndNode(id="end")
    return Graph.model_construct(
        id="g-paths", description="one worker",
        nodes=[_BeginNode(id="begin"), _AgentNodeRef(id="worker", agent_id="x", input_template="go"), end],
        edges=[_StaticEdge(from_node="begin", to_node="worker"), _StaticEdge(from_node="worker", to_node="end")],
        max_iterations=max_iterations, harness_id=None,
    )


async def open_turn(storage, io, *, reopen: bool = False, user_input: bool = True, bump: bool = False) -> WorkspaceSession:
    """``create_session`` / ``wake_session``'s records: (a reopen's ``invocation_divider``, then) a ``user_input`` (none for a restart without input); the row as the reopen leaves it."""
    sessions = storage.get_storage(WorkspaceSession)
    row = await sessions.get(SID)
    if row is None:
        row = WorkspaceSession(id=SID, workspace_id="w1", binding=GraphSessionBinding(graph_id="g-paths"), status=SessionStatus.RUNNING, created_at=_now(), turn_status="running")
        await sessions.create(row)
        row = await sessions.get(SID)
    writer = WorkspaceMessageWriter(workspace_io=io, session_id=SID, start_seq=row.last_seq)
    last = row.last_seq
    if reopen:
        last = await writer.append(SessionMessageRecord(seq=1, kind=SessionMessageKind.INVOCATION_DIVIDER, payload={"invocation": 2}, created_at=datetime.now(timezone.utc)))
    if user_input:
        last = await writer.append(SessionMessageRecord(seq=1, kind=SessionMessageKind.USER_INPUT, payload={"text": "go"}, created_at=datetime.now(timezone.utc)))
    await writer.flush()
    row = await sessions.get(SID)
    row = row.model_copy(update={
        "last_seq": last, "status": SessionStatus.RUNNING, "ended_reason": None, "ended_at": None, "turn_status": "running",
        "parked_status": None, "parked_state": None, "parked_at": None, **({"turn_no": row.turn_no + 1} if bump else {}),
    })
    await sessions.update(row)
    return row


async def run_graph(tmp_path, storage, io, bus, tl: TurnLogs, *, scripts=None, graph: Graph | None = None):
    repo = await _make_state_repo(tmp_path)
    executor = await _build_executor(graph=graph or fanout(), llm=_FakeLLM(scripts=scripts or [OK]), state_repo=repo, graph_session_id=SID, agents={"x": _agent("x")})
    outcome = await run_one_session_turn(_make_lease(SID), deps(storage, io, bus, executor, tl))
    return outcome, await storage.get_storage(WorkspaceSession).get(SID)


async def run_raising(tmp_path, storage, io, bus, tl: TurnLogs, *, after: int = 12):
    """The non-clean arm: the graph EXECUTOR raises after ``after`` events (a state-repo write that fails, say). Dispatch writes its failure exit and the claim adapter the release marker."""
    repo = await _make_state_repo(tmp_path)
    inner = await _build_executor(graph=fanout(), llm=_FakeLLM(scripts=[OK]), state_repo=repo, graph_session_id=SID, agents={"x": _agent("x")})

    class _Raising:
        def __getattr__(self, name):
            return getattr(inner, name)

        async def invoke(self, messages):
            n = 0
            async for ev in inner.invoke(messages):
                yield ev
                n += 1
                if n >= after:
                    raise RuntimeError("the state repo write failed")

    outcome = await run_one_session_turn(_make_lease(SID), deps(storage, io, bus, _Raising(), tl))
    row = await storage.get_storage(WorkspaceSession).get(SID)
    if not outcome.success:
        writer = WorkspaceMessageWriter(workspace_io=io, session_id=SID, start_seq=row.last_seq)
        await writer.append(release_marker())
        await writer.flush()
    return outcome


async def release(storage, outcome) -> WorkspaceSession:
    """What ``SessionClaimAdapter.on_release`` does to ``turn_no`` on a success (a bump); a park or a failure leaves it."""
    sessions = storage.get_storage(WorkspaceSession)
    row = await sessions.get(SID)
    if outcome.success and outcome.park is None:
        row = row.model_copy(update={"turn_no": row.turn_no + 1})
        await sessions.update(row)
    return row


class LegacyWriters:
    """The graph's end writers switched OFF: the log of a graph run as it was written before the end was a record."""

    def __enter__(self):
        self._saved = (dispatch_mod.graph_end_for, graph_resume_coordinator._write_graph_end)
        dispatch_mod.graph_end_for = lambda *a, **k: None

        async def _no_end(*a, **k):
            return None

        graph_resume_coordinator._write_graph_end = _no_end
        return self

    def __exit__(self, *exc):
        dispatch_mod.graph_end_for, graph_resume_coordinator._write_graph_end = self._saved
        return False


async def park(tmp_path, storage, io, bus, tl: TurnLogs, monkeypatch, *, scripts=None):
    """A graph that parks at an ``ask_user`` through the real dispatch; the row as the claim adapter's park branch leaves it (park columns, no bump).

    ``scripts`` is what the model says after the park, for the executors the returned factory builds (a resume): ``OK`` by default, ``FAIL`` for a resumed worker whose model call fails. The parked
    run itself never reaches the model (its ``run_agent_turn`` is patched to raise the park).
    """
    from primer.model.yield_ import Yielded, YieldToWorker
    from tests.graph.test_tool_wait_graph_park import _patch_run_agent_turn

    ask = YieldToWorker(
        Yielded(tool_name="ask_user", event_key=f"ask_user:{SID}:worker:tc-1", resume_metadata={"prompt": "color?"}),
        tool_call_id="tc-1",
        llm_messages=[{"role": "assistant", "parts": [{"type": "text", "text": "worker asking"}]}],
    )
    _patch_run_agent_turn(monkeypatch, {"x": ask})
    repo = await _make_state_repo(tmp_path)

    async def executor():
        return await _build_executor(graph=one_worker(), llm=_FakeLLM(scripts=[scripts or OK]), state_repo=repo, graph_session_id=SID, agents={"x": _agent("x")})

    parked = await run_one_session_turn(_make_lease(SID), deps(storage, io, bus, await executor(), tl))
    sessions = storage.get_storage(WorkspaceSession)
    row = (await sessions.get(SID)).model_copy(update={
        "parked_status": "parked", "parked_state": parked.park.parked_state, "parked_at": datetime.now(UTC), "parked_event_key": ask.yielded.event_key,
    })
    await sessions.update(row)
    return ask, parked, executor


async def cancel_parked(storage) -> WorkspaceSession:
    """The REAL ``cancel_session`` on a parked graph row: a WAITING row is ended inline, with no record written."""
    from primer.workspace.session_factory import SessionCancelDeps, cancel_session

    class _Sched:
        async def signal_cancel(self, sid):
            return None

    class _Registry:
        async def get_workspace(self, workspace_id):
            return None

    sessions = storage.get_storage(WorkspaceSession)
    await sessions.update((await sessions.get(SID)).model_copy(update={"status": SessionStatus.WAITING, "cancel_requested": False, "ended_reason": None}))
    await cancel_session(
        workspace_id="w1", session_id=SID,
        deps=SessionCancelDeps(storage_provider=storage, scheduler=_Sched(), claim_engine=None, event_bus=None, workspace_registry=_Registry()),
    )
    return await sessions.get(SID)


# ---- named scenarios: each returns the TurnLogs of a finished session; the records are read from the workspace io ---------------------------------------------------


async def failed_then_restarted_without_input(kind: str, tmp_path, sp, io, bus) -> TurnLogs:
    """A graph that FAILED (``kind``: a worker, the iteration limit, the executor raising), restarted with no message (``restart_session(input=None)``: a divider and NO user_input), and run to success."""
    tl = TurnLogs()
    await open_turn(sp, io)
    if kind == "worker":
        outcome, _ = await run_graph(tmp_path / "a", sp, io, bus, tl, scripts=[FAIL])
    elif kind == "iterations":
        outcome, _ = await run_graph(tmp_path / "a", sp, io, bus, tl, scripts=[OK], graph=one_worker(max_iterations=1))
    elif kind == "raised":
        outcome = await run_raising(tmp_path / "a", sp, io, bus, tl)
    else:  # pragma: no cover - a typo in a test
        raise AssertionError(kind)
    await release(sp, outcome)
    await open_turn(sp, io, reopen=True, user_input=False, bump=False)
    outcome, _ = await run_graph(tmp_path / "b", sp, io, bus, tl, scripts=[OK], graph=one_worker() if kind == "iterations" else None)
    await release(sp, outcome)
    return tl


async def legacy_then_new(invocations_after: int, tmp_path, sp, io, bus) -> TurnLogs:
    """Invocation 1 written by the OLD writers (no end record), then ``invocations_after`` invocations by the current ones, each reopened with a message."""
    tl = TurnLogs()
    await open_turn(sp, io)
    with LegacyWriters():
        outcome, _ = await run_graph(tmp_path / "old", sp, io, bus, tl, scripts=[OK])
    await release(sp, outcome)
    for n in range(invocations_after):
        await open_turn(sp, io, reopen=True)
        outcome, _ = await run_graph(tmp_path / f"new{n}", sp, io, bus, tl, scripts=[OK])
        await release(sp, outcome)
    return tl


async def parked_cancelled_then_reopened(tmp_path, sp, io, bus, monkeypatch) -> TurnLogs:
    """A graph parked at an ask_user, cancelled while parked (ended inline: no record), reopened with a message and run to success."""
    tl = TurnLogs()
    await open_turn(sp, io)
    await park(tmp_path / "a", sp, io, bus, tl, monkeypatch)
    await cancel_parked(sp)
    await open_turn(sp, io, reopen=True)
    outcome, _ = await run_graph(tmp_path / "b", sp, io, bus, tl, scripts=[OK], graph=one_worker())
    await release(sp, outcome)
    return tl


def records(io) -> list[dict]:
    return [json.loads(line) for line in io.read_lines(SID)]
