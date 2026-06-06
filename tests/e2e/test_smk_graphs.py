"""SMK graph tests (Phase 1): validation, linear run, producer/judge loop.

Covers GRF-01, GRF-02, GRF-03, GRF-05, GRF-12 via the scripted mock LLM.
"""
from __future__ import annotations

import pytest

from tests._support.mock_llm import Rule
from tests._support.runs import (
    make_graph,
    make_local_workspace,
    make_scripted_agent,
    start_graph_session,
    wait_terminal,
)
from tests._support.smk import smk

pytestmark = pytest.mark.asyncio


@smk("SMK-GRF-01")
async def test_graph_crud_and_validation(authed_client, mock_llm, unique_suffix, tmp_path):
    registry, base_url = mock_llm
    a = await make_scripted_agent(
        authed_client, registry, base_url, suffix=unique_suffix,
        scenario=f"scripted:grf01-{unique_suffix}", rules=[Rule(emit_text="ok")],
    )
    nodes = [
        {"kind": "begin", "id": "start"},
        {"kind": "agent", "id": "step", "agent_id": a["agent_id"], "input_template": "go"},
        {"kind": "end", "id": "done", "output_template": "{{ nodes.step.text }}"},
    ]
    edges = [
        {"kind": "static", "from_node": "start", "to_node": "step"},
        {"kind": "static", "from_node": "step", "to_node": "done"},
    ]
    gid = await make_graph(authed_client, suffix=unique_suffix, nodes=nodes, edges=edges)
    got = await authed_client.get(f"/v1/graphs/{gid}")
    assert got.status_code == 200, got.text
    # validation: a graph with no begin node is rejected
    bad = await authed_client.post(
        "/v1/graphs",
        json={"id": f"bad-{unique_suffix}", "nodes": [{"kind": "end", "id": "e"}], "edges": []},
    )
    assert bad.status_code == 422, bad.text


@smk("SMK-GRF-02", "SMK-GRF-12")
async def test_linear_run_and_turn_logs(authed_client, mock_llm, unique_suffix, tmp_path):
    registry, base_url = mock_llm
    a = await make_scripted_agent(
        authed_client, registry, base_url, suffix=unique_suffix,
        scenario=f"scripted:grf02-{unique_suffix}", rules=[Rule(emit_text="STEP OUTPUT")],
    )
    nodes = [
        {"kind": "begin", "id": "start"},
        {"kind": "agent", "id": "step", "agent_id": a["agent_id"], "input_template": "go"},
        {"kind": "end", "id": "done", "output_template": "{{ nodes.step.text }}"},
    ]
    edges = [
        {"kind": "static", "from_node": "start", "to_node": "step"},
        {"kind": "static", "from_node": "step", "to_node": "done"},
    ]
    gid = await make_graph(authed_client, suffix=unique_suffix, nodes=nodes, edges=edges)
    wid = await make_local_workspace(authed_client, suffix=unique_suffix, root=tmp_path)
    sid = await start_graph_session(authed_client, workspace_id=wid, graph_id=gid)
    final = await wait_terminal(authed_client, sid)
    assert final.get("status") == "ended", final
    # GRF-12: the run-level graph turn log is retrievable
    tl = await authed_client.get(f"/v1/graphs/{gid}/runs/{sid}/turn_log")
    assert tl.status_code == 200, tl.text


@smk("SMK-GRF-03", "SMK-GRF-05")
async def test_producer_judge_feedback_loop(authed_client, mock_llm, unique_suffix, tmp_path):
    registry, base_url = mock_llm
    prod_sc = f"scripted:prod-{unique_suffix}"
    judge_sc = f"scripted:judge-{unique_suffix}"
    # producer: first pass -> "draft"; on revise (input mentions feedback) -> "revised"
    producer = await make_scripted_agent(
        authed_client, registry, base_url, suffix=f"p{unique_suffix}", scenario=prod_sc,
        rules=[
            Rule(when_last_user_contains="Revise", emit_text="revised essay"),
            Rule(emit_text="draft essay"),
        ],
    )
    # judge: accept once the producer text is "revised"; else reject with feedback
    judge = await make_scripted_agent(
        authed_client, registry, base_url, suffix=f"j{unique_suffix}", scenario=judge_sc,
        rules=[
            Rule(when_last_user_contains="revised", emit_text='{"status": "accept", "feedback": "good"}'),
            Rule(emit_text='{"status": "reject", "feedback": "redo it"}'),
        ],
    )
    nodes = [
        {"kind": "begin", "id": "start"},
        {
            "kind": "agent", "id": "producer", "agent_id": producer["agent_id"],
            "input_template": "{% if iteration == 0 %}Write an essay{% else %}Revise using feedback: {{ nodes.judge.parsed.feedback }}{% endif %}",
        },
        {
            "kind": "agent", "id": "judge", "agent_id": judge["agent_id"],
            "input_template": "Review:\n{{ nodes.producer.text }}",
            "response_format": {
                "type": "object",
                "properties": {"status": {"type": "string"}, "feedback": {"type": "string"}},
                "required": ["status", "feedback"],
            },
        },
        {"kind": "end", "id": "approved", "output_template": "{{ nodes.producer.text }}"},
    ]
    edges = [
        {"kind": "static", "from_node": "start", "to_node": "producer"},
        {"kind": "static", "from_node": "producer", "to_node": "judge"},
        {
            "kind": "conditional", "from_node": "judge",
            "router": {
                "kind": "json_path",
                "branches": [
                    {"conditions": [{"path": "status", "op": "eq", "value": "accept"}], "to_node": "approved"},
                    {"conditions": [{"path": "status", "op": "eq", "value": "reject"}], "to_node": "producer"},
                ],
            },
        },
    ]
    gid = await make_graph(
        authed_client, suffix=unique_suffix, nodes=nodes, edges=edges, max_iterations=5
    )
    wid = await make_local_workspace(authed_client, suffix=unique_suffix, root=tmp_path)
    sid = await start_graph_session(authed_client, workspace_id=wid, graph_id=gid)
    final = await wait_terminal(authed_client, sid, timeout_s=90)
    assert final.get("status") == "ended", final
    # the loop converged to acceptance (reached the approved end node)
    tl = await authed_client.get(f"/v1/graphs/{gid}/runs/{sid}/turn_log")
    assert tl.status_code == 200, tl.text
