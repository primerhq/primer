"""A graph agent node whose model asks for several tool calls in ONE round, one of them approval-gated, answers every call of the round (ticket 01a15327).

The reviewer's reproduction of PR #800 (``test_m1_*``, the first case below) showed it: the gate parks the node in the middle of the round's batch, and the
park kept only the assistant message that holds every tool_use of the round. On resume the result of a call that ran BEFORE the gate was lost, a call
AFTER it never ran, and the next request carried tool_uses no result answers (asked ``[a, g, b]``, answered ``[g]``), which a strict provider (Anthropic,
OpenAI) rejects.

The pending entry now carries the round: the results of the calls that ran before the parked one and the calls after it. On resume the decision is applied
to the parked call as before, the calls after it run in order with the node's own manager (each may park again: the round is carried forward), and the
model is sent ONE tool message that answers every call of the round once, in order.

These cases drive the real ``WorkspaceGraphExecutor``, a real ``ToolExecutionManager`` with policy-gated tools and the real worker adapter
(``resume_graph_from_checkpoint``), with the harness of ``tests/graph/test_agent_node_approval_resume.py``; the last ones drive the real
``resume_graph_engine`` and a child graph's resume (``resume_invoke_graph``).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from primer.graph.invoke_graph import resume_invoke_graph
from primer.graph.workspace_executor import WorkspaceGraphExecutor
from primer.model.chat import Done, Message, StreamEvent, TextDelta, ToolCallEnd, ToolCallPart, ToolCallStart, ToolResultPart
from primer.model.tool_approval import ToolApprovalPolicy
from primer.model.yield_ import gate_id_of, with_wake_gate
from primer.toolset.system import build_system_toolset
from primer.worker import graph_resume_coordinator
from primer.worker.yield_runtime import ParkedState
from tests._resume_hook_fakes import EngineFakePool, EngineStorageProvider, NullWorkspaceIO, waiting_graph_session
from tests.agent.test_inner_dispatch import ADMIN, USER, _Recorder
from tests.graph.test_agent_node_approval_resume import GATED, _Model, _only_entry, _PausingTool, _policy, _Run, _says
from tests.toolset.test_call_tool_door import _Registry

# Re-export so pytest resolves the in-memory storage and the real provider registry the system toolset needs (the ask_user case).
from tests.toolset.test_system import pr, sp  # noqa: F401

ROUND_TOOLS = [GATED, "rec__user_a", "rec__user_b"]
SECOND_GATE = "rec__user_y"


def _asks_many(calls: list[tuple[str, str, dict[str, Any]]]) -> list[StreamEvent]:
    """One model reply that asks for every call of ``calls`` in ONE round, in that order."""
    events: list[StreamEvent] = [TextDelta(text="calling the tools", index=0)]
    for i, (call_id, name, arguments) in enumerate(calls, start=1):
        events.append(ToolCallStart(id=call_id, name=name, index=i))
        events.append(ToolCallEnd(id=call_id, arguments=arguments, index=i))
    events.append(Done(stop_reason="tool_use", raw_reason="tool_use"))
    return events


def _pairing(messages: list[Message]) -> tuple[list[str], list[str]]:
    """The ids of the tool_uses the messages hold, and the ids of the tool results that answer them, each in order."""
    asked = [p.id for m in messages if m.role == "assistant" for p in m.parts if isinstance(p, ToolCallPart)]
    answered = [p.id for m in messages if m.role == "tool" for p in m.parts if isinstance(p, ToolResultPart)]
    return asked, answered


def _results(messages: list[Message]) -> dict[str, ToolResultPart]:
    return {p.id: p for m in messages if m.role == "tool" for p in m.parts if isinstance(p, ToolResultPart)}


def _the_round(gate_at: int) -> list[tuple[str, str, dict[str, Any]]]:
    """``call-a`` and ``call-b`` (ungated) with the gated ``call-g`` at ``gate_at``."""
    order = [("call-a", "rec__user_a"), ("call-b", "rec__user_b")]
    order.insert(gate_at, ("call-g", GATED))
    return [(call_id, name, {"n": i}) for i, (call_id, name) in enumerate(order)]


def _round_run(tmp_path: Path, **kw: Any) -> tuple[_Run, _Recorder]:
    rec = _Recorder({"user_x": "user", "user_a": "user", "user_b": "user", "user_y": "user"})
    run = _Run(
        tmp_path, providers={"rec": rec}, tools=[*ROUND_TOOLS, SECOND_GATE], policies=[_policy("rec", "user_x"), _policy("rec", "user_y")], **kw,
    )
    return run, rec


def _ran(rec: _Recorder) -> list[str]:
    return [call[3].tool_call_id for call in rec.calls]


# ---- one gated call, in every position ---------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("gate_at", [0, 1, 2], ids=["gate-first", "gate-middle", "gate-last"])
@pytest.mark.parametrize("decision", ["approved", "rejected"])
async def test_every_call_of_the_round_is_answered_once_in_order(tmp_path: Path, gate_at: int, decision: str) -> None:
    run, rec = _round_run(tmp_path)
    calls = _the_round(gate_at)
    ids = [c[0] for c in calls]
    checkpoint, _live, _seq, _yld = await run.park(_Model(_asks_many(calls)))
    entry = _only_entry(checkpoint)
    assert entry["tool_call_id"] == "call-g"
    assert _ran(rec) == ids[:gate_at], "the calls before the gate ran, the gate parked the round, the calls after it did not run yet"
    assert [p["id"] for p in entry["completed_results"]] == ids[:gate_at] and [c["id"] for c in entry["remaining_calls"]] == ids[gate_at + 1:]

    model = _Model(_says("done"))
    resumed = await run.resume(model, checkpoint, entry, {"decision": decision})

    assert resumed.repark is None
    (request,) = model.requests
    asked, answered = _pairing(request)
    assert asked == ids and answered == ids, f"every call of the round must be answered once, in order: asked {asked}, answered {answered}"
    assert [m.role for m in request][-2:] == ["assistant", "tool"], "one tool message answers the whole round, right after the assistant message"
    results = _results(request)
    for call_id in ("call-a", "call-b"):
        assert (results[call_id].error, json.loads(results[call_id].output)) == (False, {"ran": f"user_{call_id[-1]}"}), "an ungated call keeps its real result"
    if decision == "approved":
        assert (results["call-g"].error, json.loads(results["call-g"].output)) == (False, {"ran": "user_x"})
        assert sorted(_ran(rec)) == sorted(ids), "every call ran once"
    else:
        assert results["call-g"].error is True and json.loads(results["call-g"].output)["rejected"] is True
        assert sorted(_ran(rec)) == sorted(i for i in ids if i != "call-g"), "the rejected call did not run, the others ran once"
    history = await resumed.executor._load_node_history("A")
    assert _pairing(history) == (ids, ids), "the node's persisted history pairs every call too"
    state = await resumed.executor.load_state()
    assert state is not None and (state["status"], state["ended_reason"]) == ("ended", "completed")


@pytest.mark.asyncio
async def test_the_park_records_what_ran_before_the_gate_and_the_resume_records_the_rest_under_their_call_rows(tmp_path: Path) -> None:
    """What ran before the gate is recorded as the round parks (it was dropped), and the calls the resume answers are recorded by the resume drain, each
    under the scoped id of its own call row (the stash keeps the ids of the calls that have not run)."""
    run, _rec = _round_run(tmp_path)
    checkpoint, live, seq, _yld = await run.park(_Model(_asks_many(_the_round(1))))
    rows = {r["payload"]["name"]: r["payload"]["id"] for r in live if r["kind"] == "tool_call"}
    assert [(r["payload"]["call_id"], r["payload"]["error"]) for r in live if r["kind"] == "tool_result"] == [(rows["rec__user_a"], False)]
    assert _only_entry(checkpoint)["remaining_scoped_call_ids"] == {"call-b": rows["rec__user_b"]}

    resumed = await run.resume(_Model(_says("done")), checkpoint, _only_entry(checkpoint), {"decision": "approved"}, seq=seq, with_tap=True)

    written = [(r["payload"]["call_id"], r["payload"]["error"], r["node_id"]) for r in resumed.records if r["kind"] == "tool_result"]
    assert written == [(rows[GATED], False, "A"), (rows["rec__user_b"], False, "A")], "the gated call's answer and the later call's result, under their rows"


@pytest.mark.asyncio
async def test_a_call_after_the_gate_that_fails_is_an_error_result_the_round_still_completes(tmp_path: Path) -> None:
    """A call that runs on resume and fails is answered with an error result (as the live batch answers it), not dropped and not a crash of the node."""
    run, rec = _round_run(tmp_path)
    calls = [("call-g", GATED, {}), ("call-z", "rec__gone", {})]               # a tool the node's manager cannot route
    checkpoint, _live, _seq, _yld = await run.park(_Model(_asks_many(calls)))

    model = _Model(_says("done"))
    resumed = await run.resume(model, checkpoint, _only_entry(checkpoint), {"decision": "approved"})

    assert resumed.repark is None
    (request,) = model.requests
    assert _pairing(request) == (["call-g", "call-z"], ["call-g", "call-z"])
    failed = _results(request)["call-z"]
    assert failed.error is True and "rec__gone" in failed.output
    assert _ran(rec) == ["call-g"]


# ---- a second gated call in the same round: two sequential parks --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("second", ["approved", "rejected"])
async def test_a_second_gated_call_of_the_round_parks_the_node_again_carrying_what_is_done(tmp_path: Path, second: str) -> None:
    run, rec = _round_run(tmp_path)
    calls = [("call-g", GATED, {}), ("call-a", "rec__user_a", {}), ("call-h", SECOND_GATE, {}), ("call-b", "rec__user_b", {})]
    ids = [c[0] for c in calls]
    checkpoint, _live, seq, _yld = await run.park(_Model(_asks_many(calls)))
    first = _only_entry(checkpoint)
    assert first["tool_call_id"] == "call-g" and rec.calls == []

    skipped = _Model(_says("the second gate was skipped"))
    resumed = await run.resume(skipped, checkpoint, first, {"decision": "approved"}, seq=seq, with_tap=True)

    assert skipped.requests == [], "the round is not complete: the model is not called"
    assert resumed.repark is not None, "the second gated call parks the node again"
    second_entry = _only_entry(resumed.repark.graph_checkpoint)
    assert (second_entry["tool_name"], second_entry["tool_call_id"]) == ("_approval", "call-h")
    assert second_entry["resume_metadata"]["original_call"]["name"] == SECOND_GATE
    assert gate_id_of(second_entry["resume_metadata"]) != gate_id_of(first["resume_metadata"]), "the second gate is a gate of its own"
    assert _ran(rec) == ["call-g", "call-a"], "the approved call and the call after it ran; the second gate parked before the last call"
    assert [m["role"] for m in second_entry["llm_messages"]] == ["assistant"], "the in-progress turn rides along: the round's assistant message, no result yet"
    assert [p["id"] for p in second_entry["completed_results"]] == ["call-g", "call-a"] and [c["id"] for c in second_entry["remaining_calls"]] == ["call-b"]
    assert second_entry["scoped_tool_call_id"] == first["remaining_scoped_call_ids"]["call-h"], "the new entry answers the second gated call's own row"
    assert second_entry["remaining_scoped_call_ids"] == {"call-b": first["remaining_scoped_call_ids"]["call-b"]}

    model = _Model(_says("done"))
    final = await run.resume(model, resumed.repark.graph_checkpoint, second_entry, {"decision": second})

    assert final.repark is None
    (request,) = model.requests
    assert _pairing(request) == (ids, ids)
    results = _results(request)
    assert json.loads(results["call-g"].output) == {"ran": "user_x"} and json.loads(results["call-a"].output) == {"ran": "user_a"}
    assert json.loads(results["call-b"].output) == {"ran": "user_b"}
    if second == "approved":
        assert json.loads(results["call-h"].output) == {"ran": "user_y"} and _ran(rec) == ["call-g", "call-a", "call-h", "call-b"]
    else:
        assert results["call-h"].error is True and json.loads(results["call-h"].output)["rejected"] is True
        assert _ran(rec) == ["call-g", "call-a", "call-b"]
    state = await final.executor.load_state()
    assert state is not None and (state["status"], state["ended_reason"]) == ("ended", "completed")


@pytest.mark.asyncio
async def test_the_re_park_of_a_second_gate_carries_the_scoped_ids_of_the_calls_that_still_wait_without_a_drain_tap(tmp_path: Path) -> None:
    """The node's re-park carries the scoped ids the entry already had for the calls that still wait: they are not left to the drain's stash, which runs
    only when the drain has a tap (a child graph's resume has none)."""
    run, _rec = _round_run(tmp_path)
    calls = [("call-g", GATED, {}), ("call-h", SECOND_GATE, {}), ("call-b", "rec__user_b", {})]
    checkpoint, _live, seq, _yld = await run.park(_Model(_asks_many(calls)))
    first = _only_entry(checkpoint)

    resumed = await run.resume(_Model(_says("skipped")), checkpoint, first, {"decision": "approved"}, seq=seq)

    assert resumed.repark is not None
    second = _only_entry(resumed.repark.graph_checkpoint)
    assert second["tool_call_id"] == "call-h"
    assert second["remaining_scoped_call_ids"] == {"call-b": first["remaining_scoped_call_ids"]["call-b"]}


@pytest.mark.asyncio
async def test_a_new_round_that_parks_gets_its_own_scoped_ids_even_when_its_raw_ids_repeat_the_old_rounds(tmp_path: Path) -> None:
    """A continued turn's NEW round can reuse the old round's raw ids (a provider numbers its calls per stream): its re-park takes the ids its own call
    rows were minted under, never the old round's."""
    run, _rec = _round_run(tmp_path)
    checkpoint, _live, seq, _yld = await run.park(_Model(_asks_many([("call-g", GATED, {}), ("call-1", "rec__user_a", {})])))
    first = _only_entry(checkpoint)
    old_scoped = first["remaining_scoped_call_ids"]["call-1"]

    model = _Model(_asks_many([("call-h", SECOND_GATE, {}), ("call-1", "rec__user_b", {})]))
    resumed = await run.resume(model, checkpoint, first, {"decision": "approved"}, seq=seq, with_tap=True)

    assert resumed.repark is not None
    second = _only_entry(resumed.repark.graph_checkpoint)
    assert (second["tool_call_id"], [c["name"] for c in second["remaining_calls"]]) == ("call-h", ["rec__user_b"])
    new_row = next(r["payload"]["id"] for r in resumed.records if r["kind"] == "tool_call" and r["payload"]["name"] == "rec__user_b")
    assert second["remaining_scoped_call_ids"] == {"call-1": new_row} and new_row != old_scoped


# ---- a later call that pauses (ask_user), and an approved call that pauses ----------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_later_ask_user_parks_the_node_on_its_question_and_its_answer_completes_the_round(
    tmp_path: Path, sp: Any, pr: Any,  # noqa: F811 - the re-exported fixtures
) -> None:
    """The call after the approved one is the system toolset's own ``ask_user``: it asks, the node waits on its key, the person's answer (through the real
    ``ask_user`` resume hook, ``graph_agent_tool_result``) is that call's result, and the call after it runs before the model is called."""
    rec = _Recorder({"user_x": "user", "user_b": "user"})
    registry = _Registry(pr, {"rec": rec})
    system = build_system_toolset(storage_provider=sp, provider_registry=registry)
    run = _Run(
        tmp_path, providers={"rec": rec, "system": system}, tools=[GATED, "system__ask_user", "rec__user_b"], policies=[_policy("rec", "user_x")], registry=registry,
    )
    calls = [("call-g", GATED, {}), ("call-q", "system__ask_user", {"prompt": "which colour?"}), ("call-b", "rec__user_b", {})]
    checkpoint, _live, _seq, _yld = await run.park(_Model(_asks_many(calls)))

    resumed = await run.resume(_Model(_says("the question was skipped")), checkpoint, _only_entry(checkpoint), {"decision": "approved"})

    assert resumed.repark is not None and _ran(rec) == ["call-g"], "the ask_user call asks: the node waits for the person"
    asking = _only_entry(resumed.repark.graph_checkpoint)
    assert (asking["tool_name"], asking["tool_call_id"], asking["event_key"]) == ("ask_user", "call-q", "ask_user:s-1:A:call-q")
    assert asking["resume_metadata"]["prompt"] == "which colour?"

    answer = await graph_resume_coordinator.graph_agent_tool_result(
        SimpleNamespace(_provider_registry=None), resumed.repark.graph_checkpoint, "call-q", {"response": "blue"}, session_id="s-1", event_key=asking["event_key"],
    )
    assert answer is not None
    model = _Model(_says("done"))
    final = await run.resume(model, resumed.repark.graph_checkpoint, asking, {"response": "blue"}, agent_tool_result=answer)

    assert final.repark is None
    (request,) = model.requests
    assert _pairing(request) == (["call-g", "call-q", "call-b"], ["call-g", "call-q", "call-b"])
    results = _results(request)
    assert json.loads(results["call-g"].output) == {"ran": "user_x"} and json.loads(results["call-q"].output) == {"response": "blue"}
    assert _ran(rec) == ["call-g", "call-b"]


@pytest.mark.asyncio
async def test_an_approved_call_that_pauses_keeps_the_rest_of_its_round(tmp_path: Path) -> None:
    """The two-phase park inside a round: the approved call itself pauses for its own event. The re-park keeps the round unchanged (the result of the call
    before it, the call after it), so its answer completes the round."""
    rec = _Recorder({"user_a": "user", "user_b": "user"})
    pausing = _PausingTool()
    run = _Run(tmp_path, providers={"rec": rec, "y": pausing}, tools=["rec__user_a", "y__wait", "rec__user_b"], policies=[_policy("y", "wait")])
    calls = [("call-a", "rec__user_a", {}), ("call-w", "y__wait", {}), ("call-b", "rec__user_b", {})]
    checkpoint, _live, _seq, _yld = await run.park(_Model(_asks_many(calls)))
    first = _only_entry(checkpoint)
    assert first["tool_name"] == "_approval" and _ran(rec) == ["call-a"]

    resumed = await run.resume(_Model(_says("the pause was skipped")), checkpoint, first, {"decision": "approved"})

    assert resumed.repark is not None and pausing.calls == ["call-w"]
    waiting = _only_entry(resumed.repark.graph_checkpoint)
    assert (waiting["tool_name"], waiting["tool_call_id"]) == ("wait", "call-w")
    assert (waiting["completed_results"], waiting["remaining_calls"]) == (first["completed_results"], first["remaining_calls"]), "the round is unchanged"

    model = _Model(_says("done"))
    answer = Message(role="tool", parts=[ToolResultPart(id="call-w", output="waited")])
    final = await run.resume(model, resumed.repark.graph_checkpoint, waiting, {"response": "ok"}, agent_tool_result=answer)

    assert final.repark is None
    (request,) = model.requests
    assert _pairing(request) == (["call-a", "call-w", "call-b"], ["call-a", "call-w", "call-b"])
    results = _results(request)
    assert json.loads(results["call-a"].output) == {"ran": "user_a"} and results["call-w"].output == "waited"
    assert _ran(rec) == ["call-a", "call-b"]


@pytest.mark.asyncio
@pytest.mark.parametrize("resumed_as", ["the-run-that-parked", "a-lesser-run"])
async def test_a_call_tool_inner_gate_in_a_round_re_runs_its_inner_tool_with_the_role_checked_and_the_round_completes(
    tmp_path: Path, sp: Any, pr: Any, resumed_as: str,  # noqa: F811 - the re-exported fixtures
) -> None:
    """The gated call is ``system__call_tool``'s inner tool, an admin one: the approval re-runs it through ``dispatch_inner_tool`` as the resumed node
    (the re-run of PRs #787 and #800), its role checked again, so a park made for an admin-initiated run and resumed by a user's run is refused
    (``forbidden``); either way the calls around it in the round are answered."""
    rec = _Recorder({"admin_x": "admin", "user_a": "user", "user_b": "user"})
    registry = _Registry(pr, {"rec": rec})
    system = build_system_toolset(storage_provider=sp, provider_registry=registry)
    pr._system_toolset_provider = system
    await sp.get_storage(ToolApprovalPolicy).create(_policy("rec", "admin_x"))
    run = _Run(tmp_path, providers={"system": system, "rec": rec}, tools=["rec__user_a", "system__call_tool", "rec__user_b"], registry=registry, initiated_by=ADMIN)
    inner = {"toolset_id": "rec", "tool_name": "admin_x", "arguments": {}}
    checkpoint, _live, _seq, _yld = await run.park(_Model(_asks_many([("call-a", "rec__user_a", {}), ("call-c", "system__call_tool", inner), ("call-b", "rec__user_b", {})])))
    entry = _only_entry(checkpoint)
    assert entry["resume_metadata"]["via_call_tool"]["toolset_id"] == "rec" and _ran(rec) == ["call-a"]

    model = _Model(_says("done"))
    resumer = run if resumed_as == "the-run-that-parked" else run.as_run_of(USER)
    resumed = await resumer.resume(model, checkpoint, entry, {"decision": "approved"})

    assert resumed.repark is None
    (request,) = model.requests
    assert _pairing(request) == (["call-a", "call-c", "call-b"], ["call-a", "call-c", "call-b"])
    answer = _results(request)["call-c"]
    if resumed_as == "the-run-that-parked":
        assert (answer.error, json.loads(answer.output)) == (False, {"ran": "admin_x"}) and _ran(rec) == ["call-a", "call-c", "call-b"]
    else:
        assert answer.error is True and json.loads(answer.output)["type"] == "forbidden", "the approval ran an admin tool for a run that is not an admin's"
        assert _ran(rec) == ["call-a", "call-b"]


# ---- a park written before the round was carried -----------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_park_without_the_round_resumes_as_it_did(tmp_path: Path) -> None:
    """A checkpoint written by an older build has no round in its entry: it resumes as before (only the parked call is answered) and nothing crashes."""
    run, rec = _round_run(tmp_path)
    checkpoint, _live, _seq, _yld = await run.park(_Model(_asks_many(_the_round(1))))
    entry = _only_entry(checkpoint)
    for key in ("completed_results", "remaining_calls", "remaining_scoped_call_ids"):
        entry.pop(key, None)

    model = _Model(_says("done"))
    resumed = await run.resume(model, checkpoint, entry, {"decision": "approved"})

    assert resumed.repark is None
    (request,) = model.requests
    assert _pairing(request) == (["call-a", "call-g", "call-b"], ["call-g"]), "an old park keeps today's behaviour"
    assert _ran(rec) == ["call-a", "call-g"], "the call after the gate is not run for a park that does not name it"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damaged",
    [
        pytest.param({"completed_results": 5, "remaining_calls": 7, "remaining_scoped_call_ids": ["call-b"]}, id="scalars-and-a-list"),
        pytest.param({"completed_results": "call-a", "remaining_calls": "call-b", "remaining_scoped_call_ids": "call-b"}, id="strings"),
    ],
)
async def test_an_entry_whose_round_fields_are_damaged_restores_as_an_old_one_and_answers_the_parked_call(
    tmp_path: Path, damaged: dict[str, Any],
) -> None:
    """Round fields of the wrong type (a damaged checkpoint) are no round: the entry restores as one from before, the resume drain (with its tap, which
    reads the stored entry too) does not fail, and the parked call alone is answered."""
    run, rec = _round_run(tmp_path)
    checkpoint, _live, seq, _yld = await run.park(_Model(_asks_many(_the_round(1))))
    entry = _only_entry(checkpoint)
    entry.update(damaged)

    model = _Model(_says("done"))
    resumed = await run.resume(model, checkpoint, entry, {"decision": "approved"}, seq=seq, with_tap=True)

    assert resumed.repark is None
    (request,) = model.requests
    assert _pairing(request) == (["call-a", "call-g", "call-b"], ["call-g"])
    assert _ran(rec) == ["call-a", "call-g"]


@pytest.mark.asyncio
async def test_the_checkpoint_round_trips_the_round_and_an_entry_from_before_restores_an_empty_one(tmp_path: Path) -> None:
    run, _rec = _round_run(tmp_path)
    checkpoint, _live, _seq, _yld = await run.park(_Model(_asks_many(_the_round(1))))
    executor = await run.executor(_Model())

    executor.restore_state(json.loads(json.dumps(checkpoint)))
    (restored,) = executor._pending_agent_yields
    assert [p["id"] for p in restored.completed_results] == ["call-a"] and [c["id"] for c in restored.remaining_calls] == ["call-b"]
    assert restored.remaining_scoped_call_ids == _only_entry(checkpoint)["remaining_scoped_call_ids"]
    assert _only_entry(executor.snapshot_state()) == _only_entry(checkpoint), "snapshot, restore, snapshot: the entry is the same"

    for key in ("completed_results", "remaining_calls", "remaining_scoped_call_ids"):
        _only_entry(checkpoint).pop(key, None)
    executor.restore_state(checkpoint)
    (old,) = executor._pending_agent_yields
    assert (old.completed_results, old.remaining_calls, old.remaining_scoped_call_ids) == ([], [], {})


# ---- the real engine, and a child graph --------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_real_graph_resume_answers_every_call_of_the_round(tmp_path: Path) -> None:
    """``resume_graph_engine`` (the worker's entry for a graph park) completes the round of the gate it resumes."""
    run, rec = _round_run(tmp_path)
    checkpoint, _live, _seq, yld = await run.park(_Model(_asks_many(_the_round(1))))
    entry = _only_entry(checkpoint)
    model = _Model(_says("done"))

    async def factory() -> WorkspaceGraphExecutor:
        return await run.executor(model)

    pool = EngineFakePool(storage=EngineStorageProvider(), workspace_io=NullWorkspaceIO(), executor_factory=factory)
    payload = with_wake_gate({"decision": "approved", "decided_by": "alice"}, gate_id_of(entry["resume_metadata"]))
    session = waiting_graph_session()
    session.parked_state = {"resume_event_payloads": {"k": {"event_key": entry["event_key"], "payload": payload}}}
    parked = ParkedState(
        yielded=yld.yielded, llm_messages=[], turn_no=0, started_at=datetime.now(UTC), tool_call_id=yld.tool_call_id, resume_event_payload=payload,
        graph_checkpoint=checkpoint,
    )

    outcome = await graph_resume_coordinator.resume_graph_engine(pool, session, parked)

    assert outcome == "ENDED:completed" and pool.repark_calls == []
    (request,) = model.requests
    assert _pairing(request) == (["call-a", "call-g", "call-b"], ["call-a", "call-g", "call-b"])
    assert _ran(rec) == ["call-a", "call-g", "call-b"]


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approved", "rejected"])
async def test_the_agent_node_of_a_child_graph_answers_every_call_of_the_round(tmp_path: Path, decision: str) -> None:
    """``invoke_graph`` resumes its child graph through ``resume_invoke_graph``: the child's agent node completes its round the same way."""
    run, rec = _round_run(tmp_path)
    checkpoint, _live, _seq, _yld = await run.park(_Model(_asks_many(_the_round(1))))
    entry = _only_entry(checkpoint)

    model = _Model(_says("done"))
    text, repark = await resume_invoke_graph(
        child=await run.executor(model), checkpoint=checkpoint, payload={"decision": decision}, resumed_tcid=entry["tool_call_id"],
        resumed_event_key=entry["event_key"],
    )

    assert repark is None and text == "done"
    (request,) = model.requests
    assert _pairing(request) == (["call-a", "call-g", "call-b"], ["call-a", "call-g", "call-b"])
    assert _ran(rec) == (["call-a", "call-g", "call-b"] if decision == "approved" else ["call-a", "call-b"])
