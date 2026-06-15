from primer.worker.frames import (
    AgentFrame, GraphFrame, AgentResumeContext,
    frames_to_jsonable, frames_from_jsonable,
)

def _agent_frame():
    return AgentFrame(
        agent_id="a1",
        llm_messages=[{"role": "assistant", "parts": []}],
        tool_call_id="tc-1",
        depth=2,
        context=AgentResumeContext(
            session_id="ses-1", workspace_id="ws-1", chat_id=None,
            principal="user-1", tools=["system__ask_user"],
        ),
    )

def test_agent_frame_roundtrips():
    f = _agent_frame()
    blob = frames_to_jsonable([f])
    back = frames_from_jsonable(blob)
    assert len(back) == 1
    g = back[0]
    assert isinstance(g, AgentFrame)
    assert g.kind == "agent"
    assert g.agent_id == "a1"
    assert g.tool_call_id == "tc-1"
    assert g.depth == 2
    assert g.context.session_id == "ses-1"
    assert g.context.tools == ["system__ask_user"]

def test_graph_frame_roundtrips():
    f = GraphFrame(graph_id="g1", gsid="gs-1", checkpoint={"k": 1}, tool_call_id="tc-2")
    back = frames_from_jsonable(frames_to_jsonable([f]))[0]
    assert isinstance(back, GraphFrame)
    assert back.kind == "graph"
    assert back.gsid == "gs-1"
    assert back.checkpoint == {"k": 1}

def test_mixed_stack_order_preserved():
    stack = [GraphFrame(graph_id="g", gsid="gs", checkpoint={}, tool_call_id="t0"), _agent_frame()]
    back = frames_from_jsonable(frames_to_jsonable(stack))
    assert [f.kind for f in back] == ["graph", "agent"]
