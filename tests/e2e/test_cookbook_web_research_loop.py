r"""Cookbook recipe #2 regression: iterative web-research loop.

A three-node research graph with a conditional back-edge:

    begin -> researcher -> extractor -> judge --(revise)--> researcher
                                              \--(accept)--> done

The researcher binds the REAL ``web`` toolset (``web_search`` / ``web_fetch``
against the live DuckDuckGo backend); the extractor and judge take no tools.
The judge carries a ``response_format`` so its verdict parses into
``nodes.judge.parsed.verdict``, which the ``json_path`` router reads to either
loop back (revise) or end (accept).

Recipe: primerhq.github.io/docs_source/cookbook/iterative-web-research.md

The judge is SCRIPTED to return ``revise`` ONCE and then ``accept`` -- so the
back-edge fires at least once and the loop then converges. Asserts:
  * the graph ends ``completed`` (all nodes ended);
  * the loop actually iterated then converged -- the researcher node ran in
    TWO distinct supersteps (its ``last_run_iteration`` advanced across the
    back-edge), and the judge's final verdict was ``accept``;
  * the judge produced VALID structured JSON (parsed into the routable verdict);
    and
  * the researcher hit the REAL web (a non-empty findings/fact body flowed
    through), asserted loosely (real content present, not exact URLs).

Loop-pass detection uses ``{% if 'judge' in nodes %}`` (NOT ``iteration == 0``)
per the recipe's headline quirk: ``iteration`` is a global superstep counter,
so it is not 0 on the researcher's first run.

Gated on ``web:duckduckgo`` so it skips cleanly where no web backend is wired.
"""
from __future__ import annotations

import json

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
from tests._support.testconfig import requires

pytestmark = [pytest.mark.asyncio, requires("web:duckduckgo")]


# A distinctive marker the researcher stamps on its REVISED (loop) pass; it
# flows researcher -> extractor -> judge, letting each scripted node tell the
# first pass from the second deterministically.
_REVISED_MARKER = "REVISED-PASS-MARKER"
_TOPIC = "the Python programming language and one notable feature"


def _read_graph_state(tmp_path, wid: str, sid: str) -> dict:
    state_file = tmp_path / wid / ".state" / "graphs" / sid / "state.json"
    assert state_file.exists(), f"graph state.json not found at {state_file}"
    return json.loads(state_file.read_text(encoding="utf-8"))


@smk("SMK-COOKBOOK-02")
async def test_research_loop_converges(
    authed_client, mock_llm, unique_suffix, tmp_path,
):
    registry, base_url = mock_llm
    sfx = unique_suffix

    # --- researcher: real web_search, then a sourced findings report. -----
    # First pass (input has NO gaps) -> plain findings.
    # Revise pass (input carries "Gaps:") -> findings stamped with the marker.
    researcher = await make_scripted_agent(
        authed_client, registry, base_url, suffix=f"res{sfx}",
        scenario=f"scripted:researcher-{sfx}",
        tools=["web__web_search", "web__web_fetch"],
        system_prompt=["You research a topic on the web and write sourced findings."],
        rules=[
            # Turn 1 (no tool result yet): hit the REAL web.
            Rule(when_tool_result=False,
                 emit_tool="web__web_search",
                 emit_args={"query": "Python programming language features",
                            "count": 3}),
            # Revise pass: the input_template injected "Gaps:" -> stamp marker.
            Rule(when_tool_result=True, when_last_user_contains="Gaps:",
                 emit_text=(
                     f"Updated findings ({_REVISED_MARKER}):\n"
                     "- Python is a high-level programming language "
                     "(source: https://www.python.org).\n"
                     "- It supports structural pattern matching "
                     "(source: https://docs.python.org).")),
            # First pass: plain sourced findings (the live search came back).
            Rule(when_tool_result=True,
                 emit_text=(
                     "Findings:\n- Python is a popular programming language "
                     "(source: https://www.python.org).")),
        ],
    )

    # --- extractor: no tools; forward the researcher findings as facts, ----
    # carrying the marker through when present so the judge can see the pass.
    extractor = await make_scripted_agent(
        authed_client, registry, base_url, suffix=f"ext{sfx}",
        scenario=f"scripted:extractor-{sfx}",
        system_prompt=["You distil sourced facts from findings."],
        rules=[
            Rule(when_last_user_contains=_REVISED_MARKER,
                 emit_text=(
                     f"Verified facts ({_REVISED_MARKER}):\n"
                     "1. Python is a high-level language "
                     "(source: https://www.python.org).\n"
                     "2. Python supports pattern matching "
                     "(source: https://docs.python.org).")),
            Rule(emit_text=(
                "Verified facts:\n1. Python is a popular programming "
                "language (source: https://www.python.org).")),
        ],
    )

    # --- judge: no tools; structured JSON verdict. Revise ONCE, then accept. -
    # The marker reaches the judge via nodes.extractor.text in its template.
    judge = await make_scripted_agent(
        authed_client, registry, base_url, suffix=f"jud{sfx}",
        scenario=f"scripted:judge-{sfx}",
        system_prompt=["You are a fact-checking judge. Return JSON only."],
        rules=[
            Rule(when_last_user_contains=_REVISED_MARKER,
                 emit_text='{"verdict": "accept", "gaps": [], "confidence": 0.9}'),
            Rule(emit_text=(
                '{"verdict": "revise", "gaps": ["needs a notable feature"], '
                '"confidence": 0.4}')),
        ],
    )

    nodes = [
        {
            "kind": "begin", "id": "start",
            "input_schema": {
                "type": "object", "required": ["topic"],
                "properties": {"topic": {"type": "string"}},
            },
        },
        {
            "kind": "agent", "id": "researcher", "agent_id": researcher["agent_id"],
            "input_template": (
                "{% if 'judge' in nodes %}Do another web_search pass to fill "
                "these gaps, then write an updated sourced findings report.\n"
                "Gaps: {{ nodes.judge.parsed.gaps | join('; ') }}\n"
                "Topic: {{ initial_input.topic }}"
                "{% else %}Research this topic with web_search, then write a "
                "short sourced findings report: {{ initial_input.topic }}"
                "{% endif %}"
            ),
        },
        {
            "kind": "agent", "id": "extractor", "agent_id": extractor["agent_id"],
            "input_template": (
                "Extract the verified, sourced facts as a numbered list:\n\n"
                "{{ nodes.researcher.text }}"
            ),
        },
        {
            "kind": "agent", "id": "judge", "agent_id": judge["agent_id"],
            "input_template": (
                "Topic: {{ initial_input.topic }}\n\nCurrent facts:\n"
                "{{ nodes.extractor.text }}\n\nReturn JSON only."
            ),
            "response_format": {
                "type": "object",
                "required": ["verdict", "gaps", "confidence"],
                "properties": {
                    "verdict": {"type": "string", "enum": ["accept", "revise"]},
                    "gaps": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number"},
                },
            },
        },
        {"kind": "end", "id": "done", "output_template": "{{ nodes.extractor.text }}"},
    ]
    edges = [
        {"kind": "static", "from_node": "start", "to_node": "researcher"},
        {"kind": "static", "from_node": "researcher", "to_node": "extractor"},
        {"kind": "static", "from_node": "extractor", "to_node": "judge"},
        {
            "kind": "conditional", "from_node": "judge",
            "router": {
                "kind": "json_path",
                "branches": [
                    {"conditions": [{"path": "verdict", "op": "eq", "value": "accept"}],
                     "to_node": "done"},
                    {"conditions": [{"path": "verdict", "op": "eq", "value": "revise"}],
                     "to_node": "researcher"},
                ],
                "default_to": "done",
            },
        },
    ]
    gid = await make_graph(
        authed_client, suffix=sfx, nodes=nodes, edges=edges, max_iterations=12,
    )

    wid = await make_local_workspace(authed_client, suffix=sfx, root=tmp_path)
    sid = await start_graph_session(
        authed_client, workspace_id=wid, graph_id=gid,
        instructions=json.dumps({"topic": _TOPIC}),
    )

    final = await wait_terminal(authed_client, sid, timeout_s=180)
    assert final.get("status") == "ended", final

    # --- The graph completed (not failed / max_iterations). --------------
    state = _read_graph_state(tmp_path, wid, sid)
    assert state["ended_reason"] == "completed", state
    nstates = state["node_states"]
    for nid in ("researcher", "extractor", "judge", "done"):
        assert nstates[nid]["status"] == "ended", (nid, nstates[nid])
        assert not nstates[nid]["error"], (nid, nstates[nid])

    # --- The loop iterated then converged. -------------------------------
    # The judge ran twice (revise then accept): the back-edge fired once.
    judge_reqs = [
        q for q in registry.requests
        if q.get("model") == f"scripted:judge-{sfx}"
    ]
    assert len(judge_reqs) >= 2, (
        f"judge ran {len(judge_reqs)} time(s); the revise back-edge did not "
        "fire (expected at least one revise then an accept)"
    )
    # The researcher re-ran via the back-edge. As the first node after begin,
    # the researcher runs at iteration 1 on a single linear pass; the ONLY way
    # its last_run_iteration advances past 1 is the conditional routing back to
    # it for a second pass. (Observed: researcher@4, after the first judge
    # verdict at iteration 3.)
    researcher_iter = nstates["researcher"]["last_run_iteration"]
    assert researcher_iter > 1, (
        f"researcher ran only once (last_run_iteration={researcher_iter}); the "
        "revise back-edge did not re-dispatch it for a second pass"
    )

    # --- The judge produced VALID structured JSON. -----------------------
    # Recover the judge's assistant turns from its committed message log and
    # PARSE them: both verdicts must be valid JSON carrying the routable
    # ``verdict`` key, the first ``revise`` and the last ``accept``.
    judge_node_dir = (
        tmp_path / wid / ".state" / "graphs" / sid / "nodes" / "judge"
    )
    assert judge_node_dir.exists(), f"judge node dir missing at {judge_node_dir}"
    judge_verdicts: list[str] = []
    for msgs_path in judge_node_dir.rglob("messages.jsonl"):
        for line in msgs_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("role") != "assistant":
                continue
            for part in obj.get("parts", []):
                if part.get("type") == "text" and part.get("text"):
                    parsed = json.loads(part["text"])  # must be valid JSON
                    assert "verdict" in parsed, parsed
                    judge_verdicts.append(parsed["verdict"])
    assert judge_verdicts, "no judge verdict JSON recovered from node output"
    assert judge_verdicts[0] == "revise", judge_verdicts
    assert judge_verdicts[-1] == "accept", judge_verdicts

    # --- The researcher hit the REAL web; real content flowed through. ----
    # The web_search tool was dispatched against the live DuckDuckGo backend;
    # a real, non-empty result is what let the researcher's text turn fire and
    # the pipeline continue. Confirm a tool_call + tool_result round-trip in
    # the researcher's committed message log.
    res_dir = tmp_path / wid / ".state" / "graphs" / sid / "nodes" / "researcher"
    res_blob = "\n".join(
        p.read_text(encoding="utf-8")
        for p in res_dir.rglob("messages.jsonl")
    )
    assert "web__web_search" in res_blob, (
        "researcher never called the real web_search tool"
    )
    assert '"tool_result"' in res_blob, (
        "no web_search tool result round-tripped back to the researcher; the "
        "live DuckDuckGo backend did not return"
    )

    # --- The converged output flowed THROUGH the back-edge. --------------
    # The graph session's final assistant message is the end node's rendered
    # output (the extractor's facts). It must carry the REVISED-pass marker,
    # proving the accepted result came from the second researcher pass (not the
    # first), and carry sourced content.
    sess_msgs = tmp_path / wid / ".state" / "sessions" / sid / "messages.jsonl"
    assert sess_msgs.exists(), f"graph session messages.jsonl missing at {sess_msgs}"
    report = sess_msgs.read_text(encoding="utf-8")
    assert _REVISED_MARKER in report, (
        "the converged final output does not carry the revised-pass marker; "
        "the back-edge did not actually feed a second researcher pass"
    )
    assert "source" in report.lower(), (
        "the final fact list carries no sourced content"
    )
