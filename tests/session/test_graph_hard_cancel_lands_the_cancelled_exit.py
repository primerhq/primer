"""A hard user Cancel of a GRAPH session lands the one cancelled exit (ticket 01a118f4; console review 2026-10-08, C-011).

``tests/session/test_hard_cancel_lands_the_cancelled_exit.py`` drives a single agent whose model call blocks. Dispatch is shared, but a graph
session differs where this clause meets it: the stream is the graph executor's superstep loop, every event is wrapped with its node id, the
nodes run as tasks of their own, and the text each node had streamed sits in the turn's coalesce buffers keyed by node. This runs the REAL
``WorkspaceGraphExecutor`` (begin -> agent node -> end) under the real ``run_one_session_turn`` with a model that streams and then never
answers, flags the row, and cancels the turn task the way the pool's ``cancel_once`` does.

What must hold: the cancel is absorbed into the cancelled exit (no ``CancelledError`` reaches the pool), the node's partial text is a durable
record attributed to its node ahead of CANCELLED, the node's blocked model call is actually cancelled (no task left running behind the
ended session), and the row, the tick and the terminal event are those of any other Cancel.

WHO cancels the node's model call. Only the superstep loop's own cleanup does, and only because it calls ``t.cancel()`` on each node task it
still holds (``_run_superstep_loop``'s ``finally``, ``primer/graph/base.py``). The cancellation delivered to the turn task lands at the loop's
``queue.get()``, so it never reaches the nodes by itself, and dispatch's ``turn_events.aclose()`` runs after that cleanup, not instead of it. The
cleanup then ``await``s each node task, and a task awaiting another task DIRECTLY passes a second ``cancel()`` on to it. So if the explicit cancel
is missing, the turn hangs in that ``await``, and any LATER cancel of the turn task (a ``wait_for`` timeout is one) reaches the node through it
and makes the outcome look right, only late. That is why the tests below do not wait on the turn task with a ``wait_for`` (whose timeout is such a
cancel): they use ``_finished``, which waits without cancelling and fails by name when the turn does not finish by itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from primer.model.chat import Message, StreamEvent, TextDelta
from primer.model.graph import Graph, _AgentNodeRef, _BeginNode, _EndNode, _StaticEdge
from primer.model.workspace_session import GraphSessionBinding, SessionMessageKind, SessionStatus, WorkspaceSession
from primer.session.dispatch import run_one_session_turn
from tests.graph.test_workspace_executor import _agent, _build_executor, _make_state_repo
from tests.session.test_dispatch import (  # noqa: F401  (fixtures are used by name)
    FakeWorkspaceIO,
    _make_lease,
    _now,
    fake_event_bus,
    fake_storage_provider,
    fake_workspace_io,
)
from tests.session.test_dispatch_interrupt import _deps


class _StreamsThenBlocks:
    """A model that streams one chunk and then never answers; it records that its stream was cancelled."""

    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def list_models(self):
        return ["m"]

    def stream(self, *, model: str, messages: list[Message], **kwargs: Any) -> AsyncIterator[StreamEvent]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[StreamEvent]:
        try:
            yield TextDelta(text="partial from the node", index=0)
            self.reached.set()
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


def _graph() -> Graph:
    return Graph(
        id="g-hard-cancel",
        description="begin -> A -> exit",
        nodes=[_BeginNode(id="begin"), _AgentNodeRef(id="A", agent_id="x"), _EndNode(id="exit")],
        edges=[_StaticEdge(from_node="begin", to_node="A"), _StaticEdge(from_node="A", to_node="exit")],
    )


# A backstop for a hang, not a measure of speed: the turn finishes in milliseconds when the cleanup runs and never when it does not.
_HANG_BACKSTOP_S = 10.0


async def _finished(task: "asyncio.Task") -> "asyncio.Task":
    """Wait for the turn task WITHOUT ever cancelling it; fail by name if it does not finish within the backstop.

    ``asyncio.wait_for`` cannot be used for this: its timeout cancels the task, and that cancel reaches a node through the superstep loop's
    ``await t`` (a task awaiting a task passes a cancel on), which hides a missing node cancel as a slow success or a TimeoutError.
    """
    done, _pending = await asyncio.wait({task}, timeout=_HANG_BACKSTOP_S)
    if not done:
        task.cancel()                                # not to leak it: this is cleanup after the verdict, not part of it
        with contextlib.suppress(BaseException):
            await asyncio.wait({task}, timeout=5.0)
        pytest.fail(
            f"the turn task was still running {_HANG_BACKSTOP_S:.0f} s after the Cancel: the superstep loop's cleanup awaits each node task "
            "and nothing cancelled the blocked ones (the `t.cancel()` in _run_superstep_loop's finally, primer/graph/base.py)",
        )
    return task


async def _seed_graph_session(storage_provider, sid: str = "gs1") -> WorkspaceSession:
    sess = WorkspaceSession(
        id=sid, workspace_id="w1", binding=GraphSessionBinding(graph_id="g-hard-cancel"),
        status=SessionStatus.RUNNING, created_at=_now(), turn_status="running",
    )
    await storage_provider.get_storage(WorkspaceSession).create(sess)
    return sess


def _records(io: FakeWorkspaceIO, sid: str) -> list[dict]:
    return [json.loads(line) for line in io.read_lines(sid)]


async def _run_and_cancel(tmp_path: Path, storage, io, bus, *, flag_the_row: bool):
    sid = (await _seed_graph_session(storage)).id
    llm = _StreamsThenBlocks()
    repo = await _make_state_repo(tmp_path)
    executor = await _build_executor(graph=_graph(), llm=llm, state_repo=repo, graph_session_id=sid, agents={"x": _agent("x")})
    before = set(asyncio.all_tasks())
    task = asyncio.ensure_future(run_one_session_turn(_make_lease(sid), _deps(storage, io, bus, executor)))
    await asyncio.wait_for(llm.reached.wait(), 5.0)
    await asyncio.sleep(0.1)                         # let the node's text reach dispatch's buffers

    if flag_the_row:
        sessions = storage.get_storage(WorkspaceSession)
        row = await sessions.get(sid)
        row.cancel_requested = True
        await sessions.update(row)
    task.cancel()                                    # the pool's cancel_once("user_signal")
    return sid, llm, task, before


class TestAUserCancelOfAGraphSession:
    async def test_it_takes_the_one_cancelled_exit_and_keeps_the_nodes_text(
        self, tmp_path, fake_workspace_io, fake_event_bus, fake_storage_provider,
    ) -> None:
        sid, llm, task, _before = await _run_and_cancel(
            tmp_path, fake_storage_provider, fake_workspace_io, fake_event_bus, flag_the_row=True,
        )
        outcome = (await _finished(task)).result()             # a CancelledError here is the bug

        assert outcome.success and outcome.drop_lease
        assert task.cancelling() == 0
        row = await fake_storage_provider.get_storage(WorkspaceSession).get(sid)
        assert row.status == SessionStatus.ENDED and row.ended_reason == "cancelled"
        records = _records(fake_workspace_io, sid)
        kinds = [r["kind"] for r in records]
        assert kinds[-1] == SessionMessageKind.CANCELLED, f"the transcript must end in CANCELLED, got {kinds}"
        assert records[-1]["payload"]["reason"] == "operator_cancel"
        streamed = [r for r in records if r["kind"] == SessionMessageKind.ASSISTANT_TOKEN]
        assert [r["payload"]["text"] for r in streamed] == ["partial from the node"]
        assert streamed[0]["node_id"] == "A", "the partial text is attributed to the node that streamed it"
        assert kinds.index(SessionMessageKind.ASSISTANT_TOKEN) < kinds.index(SessionMessageKind.CANCELLED)

    async def test_the_nodes_blocked_model_call_is_cancelled_and_nothing_runs_on_behind_the_ended_session(
        self, tmp_path, fake_workspace_io, fake_event_bus, fake_storage_provider,
    ) -> None:
        _sid, llm, task, before = await _run_and_cancel(
            tmp_path, fake_storage_provider, fake_workspace_io, fake_event_bus, flag_the_row=True,
        )
        (await _finished(task)).result()
        await asyncio.sleep(0.2)

        assert llm.cancelled.is_set(), (
            "the node's model call was never cancelled: nothing cancelled the node task (the superstep loop's cleanup does, by an explicit "
            "t.cancel(); dispatch's aclose() does not reach the node, and a cancel of the turn task stops at the loop's queue.get())"
        )
        leaked = [t for t in asyncio.all_tasks() - before if not t.done() and t is not asyncio.current_task()]
        assert not leaked, f"tasks left running behind a cancelled graph session: {leaked}"

    async def test_an_unflagged_cancel_of_a_graph_session_still_propagates(
        self, tmp_path, fake_workspace_io, fake_event_bus, fake_storage_provider,
    ) -> None:
        """A lease steal or the drain timeout cancels the task too. Not this execution's to land, graph or not."""
        sid, llm, task, _before = await _run_and_cancel(
            tmp_path, fake_storage_provider, fake_workspace_io, fake_event_bus, flag_the_row=False,
        )
        await _finished(task)
        assert task.cancelled(), "an unflagged cancel (a lease steal, the drain timeout) must reach the pool as a cancellation"

        row = await fake_storage_provider.get_storage(WorkspaceSession).get(sid)
        assert row.status != SessionStatus.ENDED
        assert SessionMessageKind.CANCELLED not in [r["kind"] for r in _records(fake_workspace_io, sid)]
        assert llm.cancelled.is_set(), (
            "the node's model call must still be cancelled when the cancellation propagates: the superstep loop's cleanup awaits the node "
            "task only after cancelling it, and the turn task is already finished here"
        )
