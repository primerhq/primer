"""Ticket 01a11057: a subagent under a graph agent node that re-yields on a NEW tool call must get its next reply.

FIXED by the sibling selection of ticket 01a11fc6-0cce (C-033 PR 3): a decision now selects the pending entry by the EVENT KEY it fired, and
``repark_graph_continuation`` does rewrite that key, so the second reply finds the entry although its ``tool_call_id`` still says ``leaf-1``.
The strict xfail this file carried while the fix was held is lifted; both tests are plain tests now. What follows is the reproduction's
original description. ``repark_graph_continuation`` rewrites the parked entry's ``event_key``, ``tool_name``, ``leaf`` and
``frames`` when a nested subagent yields again, but NOT its ``tool_call_id``; ``resume_graph_engine`` finds the nested entry by
``tool_call_id == event_key.rsplit(":", 1)[-1]``. So when the subagent asks a SECOND question on a new tool call (``leaf-2``), the
entry still says ``leaf-1``: the second reply does not find it, is delivered to the graph agent node as a flat reply instead of
walking the subagent, and the graph just re-parks. The subagent only ever sees the first answer.

Two tests: a SCENARIO test pinning that the first reply DID reach the subagent and the graph re-parked on the second
question's key, and a BEHAVIOUR test that the second reply reaches the subagent. Harness: ``test_graph_resume_typed_markers.py`` (real
``resume_graph_engine`` over fake storage; the subagent's resume and the first park are faked at their edges).
"""

from __future__ import annotations

import pytest

from primer.int.claim import ReleaseOutcome
from primer.model.yield_ import YieldToWorker

from tests._resume_hook_fakes import EngineStorageProvider
from tests.graph.test_tool_wait_graph_park import _patch_run_agent_turn
from tests.worker.test_graph_resume_typed_markers import (
    _SID,
    _apply_park,
    _ask_user,
    _first_park,
    _nested,
    _now,
    _parked_session,
    _Pool,
    _reply,
    _resume,
    _single_agent_graph,
    _subagent_frame,
    _Subagent,
)


class _ReyieldingSubagent(_Subagent):
    """Answers its first reply with a SECOND question on a new tool call (``leaf-2``), and finishes on the next."""

    async def _resume_subagent(self, *, agent_id, context, llm_messages, child_result, depth, invoke_tool_call_id):
        self.child_results.append(child_result)
        if len(self.child_results) == 1:
            yld = YieldToWorker(_ask_user("leaf-2"), tool_call_id="leaf-2")
            yld.frames = [_subagent_frame(invoke_tool_call_id)]
            raise yld
        return "subagent done"


async def _two_replies(monkeypatch) -> dict:
    _patch_run_agent_turn(monkeypatch, {"agent-a": _nested(_ask_user("leaf-1"), "leaf-1", invoke_tcid="invoke-a")})
    graph = _single_agent_graph()
    raised = await _first_park(graph)
    storage = EngineStorageProvider()
    subagent = _ReyieldingSubagent()
    pool = _Pool(storage=storage, graph=graph, subagent=subagent)
    await _parked_session(storage, raised, parked_at=_now())

    await _reply(storage, f"ask_user:{_SID}:leaf-1", {"response": "first"})
    first = await _resume(pool, storage)
    assert isinstance(first, ReleaseOutcome) and first.park is not None, "the first resume did not re-park on the second question"
    entry = first.park.parked_state["graph_checkpoint"]["pending_agent_yields"][0]
    seen = {
        "repark_key": first.park.parked_event_key,
        "entry_event_key": entry["event_key"],
        "child_results_after_first": [r.output for r in subagent.child_results],
    }

    await _apply_park(storage, first.park)
    await _reply(storage, first.park.parked_event_key, {"response": "second"})
    seen["second"] = await _resume(pool, storage)
    seen["child_results"] = [r.output for r in subagent.child_results]
    seen["agent_node_payloads"] = list(pool.agent_node_payloads)
    return seen


@pytest.mark.asyncio
async def test_scenario_the_first_reply_reached_the_subagent_and_the_graph_re_parked_on_the_second_question(monkeypatch):
    seen = await _two_replies(monkeypatch)

    assert seen["child_results_after_first"] == ['{"response": "first"}'], "the first reply did not reach the subagent"
    assert seen["repark_key"].endswith(":leaf-2") and seen["entry_event_key"] == seen["repark_key"]


@pytest.mark.asyncio
async def test_the_second_reply_to_a_subagent_that_re_yielded_on_a_new_tool_call_reaches_the_subagent(monkeypatch):
    seen = await _two_replies(monkeypatch)

    assert seen["child_results"] == ['{"response": "first"}', '{"response": "second"}'], (
        f"the subagent saw {seen['child_results']}: the second reply was delivered to the graph agent node instead "
        f"({seen['agent_node_payloads']}) and the graph answered {seen['second']!r}"
    )
