from datetime import datetime, timezone
from primer.worker.yield_runtime import ParkedState
from primer.worker.frames import AgentFrame, GraphFrame, AgentResumeContext
from primer.model.yield_ import Yielded

def _leaf(): return Yielded(tool_name="ask_user", event_key="ask_user:ses:c1")

def test_frames_roundtrip_through_parkedstate():
    ps = ParkedState(yielded=_leaf(), llm_messages=[], turn_no=1,
                     started_at=datetime.now(timezone.utc), tool_call_id="c1",
                     frames=[AgentFrame("a", [], "c1", 1, AgentResumeContext("ses","ws",None,"u",[]))])
    back = ParkedState.from_jsonable(ps.to_jsonable())
    assert len(back.frames) == 1 and back.frames[0].kind == "agent"

def test_legacy_park_without_frames_shims_to_empty():
    # A legacy agent park (or a session that yielded directly) has no nested
    # invoke_agent frames: the session's own turn lives in llm_messages, not a
    # frame. It must shim to an EMPTY stack so resume routes through the
    # existing per-tool_name switch.
    blob = ParkedState(yielded=_leaf(), llm_messages=[{"role": "assistant", "parts": []}],
                       turn_no=1, started_at=datetime.now(timezone.utc), tool_call_id="c1").to_jsonable()
    blob.pop("frames", None)
    back = ParkedState.from_jsonable(blob)
    assert back.frames == []

def test_legacy_invoke_graph_park_shims_to_one_graph_frame():
    blob = ParkedState(yielded=Yielded(tool_name="invoke_graph", event_key="tool_approval:ses:c1",
                          resume_metadata={"graph_id": "g", "sub_gsid": "gs", "child_tcid": "n"}),
                       llm_messages=[], turn_no=1, started_at=datetime.now(timezone.utc),
                       tool_call_id="c1", graph_checkpoint={"k": 1}).to_jsonable()
    blob.pop("frames", None)
    back = ParkedState.from_jsonable(blob)
    assert len(back.frames) == 1 and back.frames[0].kind == "graph"
    assert back.frames[0].gsid == "gs" and back.frames[0].checkpoint == {"k": 1}
