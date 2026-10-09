"""Fan-out siblings, the paths the first cut of C-033 PR 3 left on the raw id (console review C-033, #683 review, S-A and S-B; ticket 01a11fc6-0cce).

* S-A, the nested re-park: ``repark_graph_continuation`` rewrote the first pending agent entry whose RAW ``tool_call_id`` matched the answered one, so with two
  siblings parked inside a nested ``invoke_agent`` under one raw id, B's subagent re-yielding rewrote A's entry. It rewrites the entry the reply resolved
  (the same node and event key).
* S-B, a child graph resumed through ``GraphFrame``: the frame resumed the child with only the node's raw id and no fired key, and the executor's raw-id
  branch resumed EVERY pending entry with that id (one reply drained both siblings). The leaf's own event key rides down to the child executor
  (``resume_invoke_graph(resumed_event_key=)``), to the agent-result builder (``graph_agent_tool_result(event_key=)``) and to the approval-gate check; and a
  raw id with no key selects only the first entry that carries it, not all of them.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from primer.graph.invoke_graph import resume_invoke_graph
from primer.model.chat import Message, ToolResultPart
from primer.model.yield_ import Yielded, YieldToWorker
from primer.worker.frames import GraphFrame, Reparked
from primer.worker.graph_resume_coordinator import repark_graph_continuation
from tests.graph.test_tool_wait_graph_park import _mk_parallel_executor, _patch_run_agent_turn

RAW = "call_0"
GA, GB = "a" * 32, "b" * 32


# ---- S-A: the nested re-park rewrites the sibling that re-yielded ---------------------------------------------------------------------------------


def _nested_entry(node: str, gid: str) -> dict:
    key = f"tool_approval:gs:{node}:leaf_{node}"
    return {
        "node_id": node, "tool_call_id": RAW, "event_key": key, "tool_name": "_approval",
        "resume_metadata": {"gate_id": gid}, "llm_messages": [], "iteration": 0,
        "frames": [{"kind": "agent", "marker": f"frames-of-{node}"}], "leaf": {"event_key": key},
    }


def _repark(answered: dict, checkpoint: dict, new_key: str):
    new_leaf = Yielded(tool_name="_approval", event_key=new_key, resume_metadata={"gate_id": "c" * 32})
    outcome = SimpleNamespace(frames=[], leaf=new_leaf)
    release = repark_graph_continuation(
        None, SimpleNamespace(turn_no=1), SimpleNamespace(tool_call_id=RAW, node_tool_call_seq=None), checkpoint, answered, outcome,
    )
    return {e["node_id"]: e for e in release.park.parked_state["graph_checkpoint"]["pending_agent_yields"]}


def test_a_nested_repark_rewrites_the_sibling_that_re_yielded() -> None:
    a, b = _nested_entry("A", GA), _nested_entry("B", GB)
    checkpoint = {"pending_toolcalls": [], "pending_agent_yields": [a, b], "pending_dispatch": []}

    entries = _repark(b, checkpoint, "tool_approval:gs:B:leaf_B2")

    assert entries["A"]["event_key"] == "tool_approval:gs:A:leaf_A", "the answered sibling's re-yield rewrote the OTHER sibling's entry"
    assert entries["B"]["event_key"] == "tool_approval:gs:B:leaf_B2"
    assert entries["B"]["resume_metadata"] == {"gate_id": "c" * 32} and entries["A"]["resume_metadata"] == {"gate_id": GA}


def test_a_nested_repark_of_the_first_sibling_leaves_the_second_alone() -> None:
    a, b = _nested_entry("A", GA), _nested_entry("B", GB)
    checkpoint = {"pending_toolcalls": [], "pending_agent_yields": [a, b], "pending_dispatch": []}

    entries = _repark(a, checkpoint, "tool_approval:gs:A:leaf_A2")

    assert entries["A"]["event_key"] == "tool_approval:gs:A:leaf_A2" and entries["B"]["event_key"] == "tool_approval:gs:B:leaf_B"


def test_a_nested_repark_with_one_entry_still_rewrites_it() -> None:
    a = _nested_entry("A", GA)
    checkpoint = {"pending_toolcalls": [], "pending_agent_yields": [a], "pending_dispatch": []}

    assert _repark(a, checkpoint, "tool_approval:gs:A:leaf_A2")["A"]["event_key"] == "tool_approval:gs:A:leaf_A2"


# ---- S-B: the key rides down through GraphFrame -------------------------------------------------------------------------------------------------


def _approval_yield(node: str, gid: str) -> YieldToWorker:
    return YieldToWorker(
        Yielded(
            tool_name="_approval", event_key=f"tool_approval:gs:{node}:{RAW}",
            resume_metadata={"policy_id": "pol", "approval_type": "required", "gate_reason": "m", "approvers": None, "gate_id": gid,
                             "original_call": {"id": RAW, "name": "delete_workspace", "arguments": {"id": node}}},
        ),
        tool_call_id=RAW,
        llm_messages=[{"role": "assistant", "parts": [{"type": "text", "text": f"{node} asking"}]}],
    )


class _Services:
    def __init__(self, child, recorder: dict) -> None:
        self._child, self._recorder = child, recorder
        self.session_id = None
        self.resolve_provider = None

    async def resolve_graph(self, _graph_id):
        return None

    async def build_child_graph_executor(self, _graph, _gsid):
        return self._child

    async def graph_agent_tool_result(self, _checkpoint, tcid, _payload, *, event_key=None):
        self._recorder["agent_result_args"] = (tcid, event_key)
        return None


@pytest.mark.asyncio
async def test_a_reply_through_a_graph_frame_resumes_only_the_answered_sibling(monkeypatch) -> None:
    """The reviewer's probe: an agent session invoked a child graph whose superstep parked two agent nodes on the same raw id."""
    ex = await _mk_parallel_executor()
    _patch_run_agent_turn(monkeypatch, {"agent-a": _approval_yield("A", GA), "agent-b": _approval_yield("B", GB)})
    with pytest.raises(YieldToWorker) as first:
        async for _ev in ex.invoke([]):
            pass
    yld = first.value
    frame = GraphFrame(graph_id="g", gsid="gs", checkpoint=yld.graph_checkpoint, tool_call_id="outer", node_tcid=yld.tool_call_id)
    child = await _mk_parallel_executor()

    outcome = await frame.resume_leaf(yld.yielded, {"decision": "approved"}, _Services(child, {}))

    assert isinstance(outcome, Reparked), "one reply resumed BOTH child siblings (the child drained)"


@pytest.mark.asyncio
async def test_the_leafs_event_key_reaches_the_child_executor_and_the_agent_result_builder(monkeypatch) -> None:
    recorder: dict = {}

    async def fake_resume_invoke_graph(**kwargs):
        recorder["resume_invoke_graph"] = kwargs
        return "out", None

    import primer.worker.frames as frames_mod

    monkeypatch.setattr(frames_mod, "resume_invoke_graph", fake_resume_invoke_graph)
    frame = GraphFrame(graph_id="g", gsid="gs", checkpoint={"pending_agent_yields": []}, tool_call_id="outer", node_tcid=RAW)
    leaf = Yielded(tool_name="_approval", event_key=f"tool_approval:gs:B:{RAW}", resume_metadata={})

    await frame.resume_leaf(leaf, {"decision": "approved"}, _Services("child", recorder))

    assert recorder["resume_invoke_graph"]["resumed_event_key"] == f"tool_approval:gs:B:{RAW}"
    assert recorder["agent_result_args"] == (RAW, f"tool_approval:gs:B:{RAW}")


@pytest.mark.asyncio
async def test_resume_invoke_graph_hands_the_key_to_the_child_executor() -> None:
    seen: dict = {}

    class _Child:
        async def resume_from_checkpoint(self, checkpoint, **kwargs):
            seen.update(kwargs)
            return
            yield  # pragma: no cover - an async generator that yields nothing

    await resume_invoke_graph(
        child=_Child(), checkpoint={}, payload={"decision": "approved"}, resumed_tcid=RAW, resumed_event_key=f"tool_approval:gs:B:{RAW}",
        agent_tool_result=Message(role="tool", parts=[ToolResultPart(id=RAW, output="x")]),
    )

    assert seen["resumed_tcid"] == RAW and seen["resumed_event_key"] == f"tool_approval:gs:B:{RAW}"


# ---- the executor: a raw id with no key selects only the first entry that carries it ---------------------------------------------------------------


async def _two_parked(monkeypatch):
    ex = await _mk_parallel_executor()
    _patch_run_agent_turn(monkeypatch, {"agent-a": _approval_yield("A", GA), "agent-b": _approval_yield("B", GB)})
    with pytest.raises(YieldToWorker) as first:
        async for _ev in ex.invoke([]):
            pass
    return first.value.graph_checkpoint


@pytest.mark.asyncio
async def test_a_raw_id_without_a_key_resumes_only_the_first_entry_that_carries_it(monkeypatch, caplog) -> None:
    import logging

    checkpoint = await _two_parked(monkeypatch)
    ex = await _mk_parallel_executor()
    answer = Message(role="tool", parts=[ToolResultPart(id=RAW, output="approved")])

    with caplog.at_level(logging.WARNING), pytest.raises(YieldToWorker) as repark:
        async for _ev in ex.resume_from_checkpoint(checkpoint, resumed_tcid=RAW, agent_tool_result=answer):
            pass

    assert [e["node_id"] for e in repark.value.graph_checkpoint["pending_agent_yields"]] == ["B"], "A (the first) was resumed, B is still waiting"
    assert any("share" in r.getMessage() for r in caplog.records), "a raw id that names several entries is logged"


# ---- the approval-gate check judges the leaf's own entry ----------------------------------------------------------------------------------------


def _check_checkpoint() -> dict:
    """Sibling A is an agent node parked on ask_user, sibling B one parked on an approval gate; both carry the raw id ``call_0``."""
    return {
        "pending_toolcalls": [],
        "pending_agent_yields": [
            {"node_id": "A", "tool_call_id": RAW, "event_key": f"ask_user:gs:A:{RAW}", "tool_name": "ask_user", "resume_metadata": {}},
            {"node_id": "B", "tool_call_id": RAW, "event_key": f"tool_approval:gs:B:{RAW}", "tool_name": "_approval", "resume_metadata": {}},
        ],
    }


def _parked_on(leaf_key: str):
    # A graph park labels EVERY leaf "_approval" (primer/graph/_checkpoint.py), whatever it really is.
    checkpoint = _check_checkpoint()
    frame = GraphFrame(graph_id="g", gsid="gs", checkpoint=checkpoint, tool_call_id="outer", node_tcid=RAW)
    return SimpleNamespace(yielded=Yielded(tool_name="_approval", event_key=leaf_key, resume_metadata={}), frames=[frame])


def test_an_ask_user_leaf_beside_an_approval_sibling_is_not_an_approval_gate() -> None:
    from primer.worker.session_resume_coordinator import _leaf_is_an_approval_gate

    assert _leaf_is_an_approval_gate(None, _parked_on(f"ask_user:gs:A:{RAW}")) is False


def test_an_approval_leaf_beside_an_ask_user_sibling_is_an_approval_gate() -> None:
    """The raw-id scan met sibling A first and called B's real approval a non-approval: no audit record was written for the decision."""
    from primer.worker.session_resume_coordinator import _leaf_is_an_approval_gate

    assert _leaf_is_an_approval_gate(None, _parked_on(f"tool_approval:gs:B:{RAW}")) is True
