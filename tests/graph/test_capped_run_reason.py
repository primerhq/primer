"""Every reader of a graph run's outcome reads a capped run as a failed one (lead ruling 4 of ticket 01a16d48).

A run that ``max_iterations`` stops now records ``ended_reason="max_iterations_exceeded"`` (the value ``GraphThread`` allows and
``primer/graph/base.py`` documents), not ``failed``; ``ended_detail`` keeps the code. ``tests/graph/test_max_iterations.py`` pins the thread row.
These pin the readers of the executor's own outcome (``_last_ended_reason``): the session's turn driver, the graph's end record, and an invoked
child graph's failure. A real capped run drives each, not a stand-in.
"""

from __future__ import annotations

from primer.graph.executor import GraphExecutor
from primer.graph.invoke_graph import ChildGraphFailed, failure_without_an_event
from primer.model.agent import Agent
from primer.model.chat import Done, TextDelta
from primer.model.graph import (
    BranchCondition,
    Graph,
    GraphNodeMessage,
    GraphThread,
    JsonPathBranch,
    _AgentNodeRef,
    _BeginNode,
    _ConditionalEdge,
    _EndNode,
    _JsonPathRouter,
    _StaticEdge,
)
from primer.session.graph_end import graph_end_for
from primer.worker.drivers import _GraphTurnDriver
from tests.graph.test_max_iterations import _agent, _FakeLLM, _InMemoryStorage, _model


async def _capped_run() -> tuple[GraphExecutor, GraphThread]:
    """begin -> a, and a routes back to itself until the cap (2) stops the run; no ``on_max_iterations``."""
    graph = Graph(
        id="g-capped",
        description="a loop the cap ends",
        max_iterations=2,
        nodes=[_BeginNode(id="b"), _AgentNodeRef(id="a", agent_id="x", response_format={"type": "object"}), _EndNode(id="exit")],
        edges=[
            _StaticEdge(from_node="b", to_node="a"),
            _ConditionalEdge(
                from_node="a",
                router=_JsonPathRouter(
                    branches=[JsonPathBranch(conditions=[BranchCondition(path="go", op="eq", value="a")], to_node="a")],
                    default_to="exit",
                ),
            ),
        ],
    )
    llm = _FakeLLM(scripts=[[TextDelta(text='{"go": "a"}', index=0), Done(stop_reason="stop", raw_reason="stop")]])

    async def agent_resolver(agent_id: str) -> Agent:
        return _agent(agent_id)

    async def llm_resolver(_agent: Agent):
        return llm, _model()

    threads: _InMemoryStorage[GraphThread] = _InMemoryStorage(GraphThread)
    messages: _InMemoryStorage[GraphNodeMessage] = _InMemoryStorage(GraphNodeMessage)
    thread = await GraphExecutor.open_thread(graph=graph, thread_storage=threads, title="t")  # type: ignore[arg-type]
    executor = GraphExecutor(
        graph=graph, agent_resolver=agent_resolver, llm_resolver=llm_resolver,  # type: ignore[arg-type]
        thread_storage=threads, message_storage=messages, graph_thread_id=thread.id,  # type: ignore[arg-type]
    )
    _ = [event async for event in executor.invoke([])]
    stored = await threads.get(thread.id)
    assert stored is not None
    return executor, stored


async def test_the_capped_run_records_its_own_reason_and_keeps_the_detail():
    executor, thread = await _capped_run()

    assert (thread.ended_reason, thread.ended_detail) == ("max_iterations_exceeded", "max_iterations_exceeded")
    assert (executor._last_ended_reason, executor._last_ended_detail) == ("max_iterations_exceeded", "max_iterations_exceeded")


async def test_the_session_turn_driver_reads_a_capped_run_as_failed():
    """``_GraphTurnDriver`` maps any outcome but ``completed`` to ``graph_failed``, so the session row still ends ``failed``."""
    executor, _ = await _capped_run()

    assert _GraphTurnDriver(executor).last_done_reason == "graph_failed"
    assert executor.last_done_reason == "graph_failed"


async def test_the_end_record_of_a_capped_run_is_a_failure_that_names_the_cap():
    executor, _ = await _capped_run()
    driver = _GraphTurnDriver(executor)

    record = graph_end_for(driver.last_done_reason, driver)

    assert record is not None
    assert record.payload == {"stop_reason": "error", "raw_reason": "graph_failed", "graph_end": True, "ended_reason": "max_iterations_exceeded"}


async def test_an_invoked_child_the_cap_ended_reads_as_failed_from_its_outcome():
    """``run_invoke_graph`` decides a failure by the child's error event first, and a capped run always yields one; the outcome reader
    (``failure_without_an_event``) matched on ``failed`` and would have read the new reason as a success."""
    executor, _ = await _capped_run()

    failed = failure_without_an_event(executor)

    assert isinstance(failed, ChildGraphFailed)
    assert (failed.code, failed.node_id) == ("max_iterations_exceeded", None)
