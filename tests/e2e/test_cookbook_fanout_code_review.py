"""Cookbook recipe #8 regression: fan-out code review.

A graph fans the same code out to two scripted reviewer agents (bugs, style)
via a ``fan_out`` (tee) node, then a ``fan_in`` aggregator concatenates both
findings into a single report.

Recipe: primerhq.github.io/docs_source/cookbook/fanout-code-review.md

Asserts (the recipe's verified outcome):
  * the graph ends ``completed``,
  * both reviewer node instances ran in the SAME superstep (identical
    ``last_run_iteration`` in the on-disk graph state), and
  * the aggregated fan_in report is non-empty and carries both reviewers'
    distinctive findings.

A tee node's output is a one-element LIST, so the fan_in template reads
``{{ nodes.review_bugs[0].text }}`` (NOT ``.text``) -- the recipe's headline
quirk, pinned here by using the list-indexed accessor.

Uses the scripted mock LLM (deterministic Rules), not a real model.
"""
from __future__ import annotations

import json
from pathlib import Path

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


_BUGS_FINDING = "ZeroDivisionError when b is zero"
_STYLE_FINDING = "return the result directly and add a docstring"


def _read_graph_state(tmp_path: Path, wid: str, sid: str) -> dict:
    """Read the on-disk graph state.json for a local-workspace graph run.

    The local backend roots the workspace at ``<provider_root>/<wid>`` and the
    graph executor commits state under ``.state/graphs/<sid>/state.json``.
    """
    state_file = (
        tmp_path / wid / ".state" / "graphs" / sid / "state.json"
    )
    assert state_file.exists(), f"graph state.json not found at {state_file}"
    return json.loads(state_file.read_text(encoding="utf-8"))


@smk("SMK-COOKBOOK-08")
async def test_parallel_reviewers_aggregate(
    authed_client, mock_llm, unique_suffix, tmp_path,
):
    registry, base_url = mock_llm

    # Two scripted reviewer agents, each emitting one distinctive finding.
    bugs = await make_scripted_agent(
        authed_client, registry, base_url, suffix=f"b{unique_suffix}",
        scenario=f"scripted:rev-bugs-{unique_suffix}",
        system_prompt=["Review code for bugs only."],
        rules=[Rule(emit_text=_BUGS_FINDING)],
    )
    style = await make_scripted_agent(
        authed_client, registry, base_url, suffix=f"s{unique_suffix}",
        scenario=f"scripted:rev-style-{unique_suffix}",
        system_prompt=["Review code for style only."],
        rules=[Rule(emit_text=_STYLE_FINDING)],
    )

    nodes = [
        {
            "kind": "begin", "id": "start",
            "input_schema": {
                "type": "object", "required": ["code"],
                "properties": {"code": {"type": "string"}},
            },
        },
        {
            "kind": "fan_out", "id": "split",
            "specs": [{"kind": "tee",
                       "target_node_ids": ["review_bugs", "review_style"]}],
        },
        {
            "kind": "agent", "id": "review_bugs", "agent_id": bugs["agent_id"],
            "input_template": "Review this code for BUGS only:\n{{ initial_input.code }}",
        },
        {
            "kind": "agent", "id": "review_style", "agent_id": style["agent_id"],
            "input_template": "Review this code for STYLE only:\n{{ initial_input.code }}",
        },
        {
            # Tee output is a LIST: index [0] to read each target's text.
            "kind": "fan_in", "id": "combine",
            "aggregate_template": (
                "## Bugs\n{{ nodes.review_bugs[0].text }}\n\n"
                "## Style\n{{ nodes.review_style[0].text }}"
            ),
        },
        {"kind": "end", "id": "done", "output_template": "{{ nodes.combine.text }}"},
    ]
    edges = [
        {"kind": "static", "from_node": "start", "to_node": "split"},
        {"kind": "static", "from_node": "review_bugs", "to_node": "combine"},
        {"kind": "static", "from_node": "review_style", "to_node": "combine"},
        {"kind": "static", "from_node": "combine", "to_node": "done"},
    ]
    gid = await make_graph(
        authed_client, suffix=unique_suffix, nodes=nodes, edges=edges,
        max_iterations=10,
    )

    wid = await make_local_workspace(authed_client, suffix=unique_suffix, root=tmp_path)
    sid = await start_graph_session(
        authed_client, workspace_id=wid, graph_id=gid,
        instructions=json.dumps({"code": "def divide(a, b):\n    return a / b"}),
    )

    final = await wait_terminal(authed_client, sid, timeout_s=90)
    assert final.get("status") == "ended", final

    # The graph completed (not failed / max_iterations).
    state = _read_graph_state(tmp_path, wid, sid)
    assert state["ended_reason"] == "completed", state

    # Both reviewer instances ran, and ran in the SAME superstep -- a tee
    # dispatches all targets in one iteration, so their last_run_iteration
    # must match.
    nodestates = state["node_states"]
    assert nodestates["review_bugs"]["status"] == "ended", nodestates
    assert nodestates["review_style"]["status"] == "ended", nodestates
    bugs_iter = nodestates["review_bugs"]["last_run_iteration"]
    style_iter = nodestates["review_style"]["last_run_iteration"]
    assert bugs_iter == style_iter, (
        f"reviewers ran in different supersteps: bugs@{bugs_iter} "
        f"style@{style_iter}"
    )

    # Each reviewer made an LLM pass (its node turn_log is retrievable + non-empty).
    for node_id in ("review_bugs", "review_style"):
        tl = await authed_client.get(
            f"/v1/graphs/{gid}/runs/{sid}/nodes/{node_id}/turn_log"
        )
        assert tl.status_code == 200, tl.text
        assert tl.json()["items"], f"no turns logged for {node_id}: {tl.text}"

    # The aggregated fan_in report is non-empty and carries BOTH findings.
    # The end node's rendered output is committed as the graph session's final
    # assistant message under .state/sessions/<sid>/messages.jsonl. That the
    # graph reached this point at all (ended_reason=completed, above) is itself
    # proof the list-indexed tee accessor rendered -- the wrong `.text`
    # accessor would have ended the run with ended_reason=template_error.
    session_msgs = (
        tmp_path / wid / ".state" / "sessions" / sid / "messages.jsonl"
    )
    assert session_msgs.exists(), f"session messages.jsonl missing at {session_msgs}"
    report = session_msgs.read_text(encoding="utf-8")
    assert "## Bugs" in report and "## Style" in report, (
        f"aggregated report missing the fan_in section headers: {report!r}"
    )
    assert _BUGS_FINDING in report, f"bugs finding missing from aggregate: {report!r}"
    assert _STYLE_FINDING in report, f"style finding missing from aggregate: {report!r}"
