"""The logs a graph turn writes, produced by the REAL writers (ticket 01a11f35).

A graph session turn is a user input, then the records of every node (each carries its ``node_id``), then the run's own end written by session dispatch (no ``node_id``). The node records
come from a real ``GraphExecutor`` fan-out (two workers that call a delegating tool and then answer) through the real ``translate_stream_event``, the delegated runs through the real
``run_subagent`` and ``DelegationRecorder`` (the scenes of ``tests/graph/test_fanout_delegation_order.py``, which this module builds on). What dispatch and the claim adapter write around
them is built the way ``dispatch._end_turn_failed`` and ``claim/adapters/sessions._write_terminal_record`` shape it (``failure_exit`` and ``release_marker`` below; the production writers
themselves are driven for a non-graph turn by ``test_failed_turn_is_one_window.py``).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from primer.graph.executor import GraphExecutor
from primer.model.chat import Done, Error
from primer.model.graph import FanOutSpec, Graph, _AgentNodeRef, _BeginNode, _EndNode, _FanInNode, _FanOutNode, _StaticEdge
from primer.model.workspace_session import SessionMessageKind, SessionMessageRecord
from primer.session.delegation import DelegationRecorder, reset_delegation_sink, set_delegation_sink
from primer.session.persistence import _CoalesceState, translate_stream_event
from tests.graph import test_fanout_delegation_order as scene
from tests.graph.test_fanout_delegation_order import T, _as_list, _Bus, _Log, _WorkerLLM

FAILURE = "provider exploded"


class FailingWorkerLLM(_WorkerLLM):
    """A worker whose SECOND model call (the answer after its tool round) fails with a fatal stream error.

    ``fail_answers`` are the 1-based positions, in the order the workers reach their answer, of the workers that fail (the first and the second of the two); empty means every worker.
    """

    def __init__(self, fail_answers: tuple[int, ...] = ()) -> None:
        self._fail_answers = fail_answers
        self._answers = 0

    def stream(self, *, model, messages, **kw: Any):
        if any(m.role == "tool" for m in messages):
            self._answers += 1
            if not self._fail_answers or self._answers in self._fail_answers:

                async def gen():
                    await asyncio.sleep(0)
                    yield Error(message=FAILURE, code="server_error", fatal=True)

                return gen()
        return super().stream(model=model, messages=messages, **kw)


def _fanout_graph(on_failure: str) -> Graph:
    return Graph.model_construct(
        id="g", description="fan-out delegation",
        nodes=[
            _BeginNode(id="begin"),
            _FanOutNode(id="fan", specs=[FanOutSpec(kind="broadcast", target_node_id="worker", count=2, on_failure=on_failure)]),
            _AgentNodeRef(id="worker", agent_id="ag", input_template="W{{ fanout_index }}"),
            _FanInNode(id="agg", aggregate_template="{% for n in nodes.worker %}{{ n.text }}{% endfor %}"),
            _EndNode(id="end", output_template="{{ nodes.agg.text }}"),
        ],
        edges=[_StaticEdge(from_node="begin", to_node="fan"), _StaticEdge(from_node="worker", to_node="agg"), _StaticEdge(from_node="agg", to_node="end")],
        max_iterations=10, harness_id=None,
    )


def release_marker(reason: str = "unknown") -> SessionMessageRecord:
    """The claim adapter's bare release marker (``_write_terminal_record``): an ERROR with ``{"reason": ..., "terminal": True}`` and no node."""
    return SessionMessageRecord(seq=1, kind=SessionMessageKind.ERROR, payload={"reason": reason, "terminal": True}, created_at=T)


def failure_exit(message: str = FAILURE) -> SessionMessageRecord:
    """Dispatch's failure exit (``_end_turn_failed``): an ERROR with the problem-details words, a ``title`` and an integer ``status``, no ``fatal``, no node."""
    return SessionMessageRecord(
        seq=1, kind=SessionMessageKind.ERROR,
        payload={"message": message, "code": "/errors/internal", "title": "Internal error", "status": 500, "extensions": {}}, created_at=T,
    )


def cancelled(reason: str = "user") -> SessionMessageRecord:
    """Dispatch's CANCELLED record for a Stop (``_cancelled_record``): ``{"reason": ...}`` and no node."""
    return SessionMessageRecord(seq=1, kind=SessionMessageKind.CANCELLED, payload={"reason": reason}, created_at=T)


def with_park(records: list[dict], after_seq: int) -> list[dict]:
    """``records`` with a park and its resume inserted after ``after_seq``: a graph that parks writes ``yielded`` when it waits and ``resumed`` when the answer arrives, and neither is a terminal."""
    out: list[dict] = []
    for rec in records:
        out.append(rec)
        if rec["seq"] == after_seq:
            for kind in ("yielded", "resumed"):
                out.append({"seq": 0, "kind": kind, "node_id": None, "payload": {"event_key": "k"}, "created_at": rec["created_at"]})
    return [dict(rec, seq=index + 1) for index, rec in enumerate(out)]


async def play(
    executor: GraphExecutor, log: _Log | None = None, *, user_text: str = "go", finish: str = "done",
) -> list[dict]:
    """The records session dispatch writes for one turn of this executor (as ``_run`` of the delegation-order scene does), then the turn's own end.

    ``finish``: ``"done"`` the run's final ``done`` (a graph that finished), ``"failed"`` dispatch's failure exit and the release marker (the executor raised or ended on a failed node),
    ``"marker"`` the release marker alone (a worker that died: the lease was lost), ``"none"`` nothing (a turn that is still running). A stream that raises is caught: what it wrote is the log.
    """
    log, state = log if log is not None else _Log(), _CoalesceState()
    executor.bind_coalesce_state(state)
    log.add(SessionMessageRecord(seq=1, kind=SessionMessageKind.USER_INPUT, payload={"text": user_text}, created_at=T))
    token = set_delegation_sink(DelegationRecorder(writer=log, event_bus=_Bus(), session_id="s", turn_no=1))
    try:
        async for ev in executor.invoke([]):
            for rec in _as_list(translate_stream_event(ev, state, turn_no=1)):
                await log.append(rec)
    except Exception:  # noqa: BLE001 - a failing graph raises out of the stream: what it wrote before is the log
        pass
    finally:
        reset_delegation_sink(token)
    if finish == "done":
        for rec in _as_list(translate_stream_event(Done(stop_reason="stop", raw_reason="stop"), state, turn_no=1)):
            log.add(rec)
    elif finish == "failed":
        log.add(failure_exit())
        log.add(release_marker())
    elif finish == "marker":
        log.add(release_marker())
    return [r.model_dump(mode="json") for r in log.records]


async def graph_turn(
    *, llm: Any | None = None, depth: int = 0, on_failure: str = "fail_fast", finish: str = "done", monkeypatch: pytest.MonkeyPatch,
) -> list[dict]:
    """One graph turn through the real executor: the two-worker fan-out, ``depth`` subgraph nodes deep, with the fan-out policy ``on_failure``."""
    monkeypatch.setattr(scene, "_fanout_graph", lambda workers=2: _fanout_graph(on_failure))
    executor = await scene._executor(llm or _WorkerLLM(), depth=depth, monkeypatch=monkeypatch)
    return await play(executor, finish=finish)
