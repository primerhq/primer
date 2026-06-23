r"""Cookbook recipe (CLI path): iterative web-research loop, driven by primectl.

The ``primectl``-driven sibling of ``test_cookbook_web_research_loop``. Every
setup step is the exact ``primectl`` command the rewritten doc shows:

  * ``create -f`` the scripted LLM provider and the three agents (researcher,
    extractor, judge);
  * ``create -f`` the conditional-loop graph manifest (begin -> researcher ->
    extractor -> judge with a ``json_path`` back-edge), the way the doc leads
    the graph step with a CLI manifest / the editor's Import-spec paste;
  * ``create -f`` / ``--set`` the local workspace; and
  * ``session run --graph ... --graph-input`` the topic, then ``workspace files
    get`` to read the on-disk graph state + node logs + the final report back.

The success outcome asserted mirrors the API test's: the graph ends
``completed``, the back-edge fired (the researcher re-ran in a later superstep
and the judge ran twice -- revise then accept), the judge emitted valid
structured JSON, and the researcher hit the REAL web (a ``web__web_search``
tool_call round-tripped in its node log).

The researcher binds the REAL ``web`` toolset (``web_search`` against the live
DuckDuckGo backend); the three agents' LLM behaviour is scripted via the shared
in-process ``mock_llm`` (deterministic Rules). Gated on ``web:duckduckgo`` so it
skips cleanly where no web backend is wired.

Recipe: primerhq.github.io/docs_source/cookbook/iterative-web-research.md

Run with:
    PRIMER_RUN_E2E=1 uv run pytest \
        tests/e2e/test_cookbook_iterative_web_research_cli.py -n0 -q
"""
from __future__ import annotations

import json

import pytest

from tests._support.mock_llm import Rule
from tests._support.primectl_driver import Primectl, manifest, mint_token
from tests._support.smk import smk
from tests._support.testconfig import requires

pytestmark = [requires("web:duckduckgo")]


# A distinctive marker the researcher stamps on its REVISED (loop) pass; it
# flows researcher -> extractor -> judge, letting each scripted node tell the
# first pass from the second deterministically.
_REVISED_MARKER = "REVISED-PASS-MARKER"
_TOPIC = "the Python programming language and one notable feature"


def _files_get(pc: Primectl, wid: str, rel: str) -> str:
    return pc.run("workspace", "files", "get", wid, rel, "--content").stdout


@smk("SMK-COOKBOOK-CLI-08")
def test_iterative_web_research_cli(base_url, mock_llm, unique_suffix, tmp_path):
    registry, mock_base_url = mock_llm
    sfx = unique_suffix
    pc = Primectl(base_url, mint_token(base_url, name=f"cli-research-{sfx}"))

    pid = f"p-research-cli-{sfx}"
    res_id = f"web-researcher-cli-{sfx}"
    ext_id = f"fact-extractor-cli-{sfx}"
    jud_id = f"research-judge-cli-{sfx}"
    gid = f"research-loop-cli-{sfx}"
    wp = f"wp-research-cli-{sfx}"
    tpl = f"tpl-research-cli-{sfx}"

    # One scripted scenario per agent. The researcher hits the REAL web on its
    # first turn, then stamps the revised-pass marker once the judge's "Gaps:"
    # input arrives on the second pass.
    res_scn = f"scripted:research-res-cli-{sfx}"
    ext_scn = f"scripted:research-ext-cli-{sfx}"
    jud_scn = f"scripted:research-jud-cli-{sfx}"
    registry.register(res_scn, [
        # Turn 1 (no tool result yet): hit the REAL web.
        Rule(when_tool_result=False, emit_tool="web__web_search",
             emit_args={"query": "Python programming language features", "count": 3}),
        # Revise pass: the input_template injected "Gaps:" -> stamp the marker.
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
    ])
    registry.register(ext_scn, [
        Rule(when_last_user_contains=_REVISED_MARKER,
             emit_text=(
                 f"Verified facts ({_REVISED_MARKER}):\n"
                 "1. Python is a high-level language "
                 "(source: https://www.python.org).\n"
                 "2. Python supports pattern matching "
                 "(source: https://docs.python.org).")),
        Rule(emit_text=(
            "Verified facts:\n1. Python is a popular programming language "
            "(source: https://www.python.org).")),
    ])
    registry.register(jud_scn, [
        Rule(when_last_user_contains=_REVISED_MARKER,
             emit_text='{"verdict": "accept", "gaps": [], "confidence": 0.9}'),
        Rule(emit_text=(
            '{"verdict": "revise", "gaps": ["needs a notable feature"], '
            '"confidence": 0.4}')),
    ])

    cleanup = [("graph", gid), ("agent", res_id), ("agent", ext_id),
               ("agent", jud_id), ("llm_provider", pid)]
    try:
        # --- The scripted LLM provider (lists all three scenarios) -------
        pc.run("create", "-f", manifest(tmp_path, "llm", "llm_provider", {
            "id": pid, "provider": "openchat",
            "models": [
                {"name": res_scn, "context_length": 8192},
                {"name": ext_scn, "context_length": 8192},
                {"name": jud_scn, "context_length": 8192},
            ],
            "config": {"url": mock_base_url, "flavor": "lmstudio"},
            "limits": {"max_concurrency": 4},
        }))

        # --- The three agents (create -f) --------------------------------
        pc.run("create", "-f", manifest(tmp_path, "researcher", "agent", {
            "id": res_id,
            "description": "Searches the web and writes a sourced findings report.",
            "model": {"provider_id": pid, "model_name": res_scn},
            "tools": ["web__web_search", "web__web_fetch"],
            "system_prompt": [
                "You research a topic on the web and write short sourced "
                "findings. Use web_search to find sources; do not fabricate."
            ],
        }))
        pc.run("create", "-f", manifest(tmp_path, "extractor", "agent", {
            "id": ext_id,
            "description": "Distils sourced facts from research findings.",
            "model": {"provider_id": pid, "model_name": ext_scn},
            "tools": [],
            "system_prompt": [
                "You distil research findings into a clean numbered list of "
                "atomic facts, each with its source URL."
            ],
        }))
        pc.run("create", "-f", manifest(tmp_path, "judge", "agent", {
            "id": jud_id,
            "description": "Judges whether the research is complete.",
            "model": {"provider_id": pid, "model_name": jud_scn},
            "tools": [],
            "system_prompt": [
                "You are a fact-checking judge. Return JSON only: verdict is "
                "accept or revise; gaps lists what is missing; confidence 0-1."
            ],
        }))

        # --- The conditional research-loop graph (create -f manifest) ----
        nodes = [
            {"kind": "begin", "id": "start", "input_schema": {
                "type": "object", "required": ["topic"],
                "properties": {"topic": {"type": "string"}}}},
            {"kind": "agent", "id": "researcher", "agent_id": res_id,
             "input_template": (
                 "{% if 'judge' in nodes %}Do another web_search pass to fill "
                 "these gaps, then write an updated sourced findings report.\n"
                 "Gaps: {{ nodes.judge.parsed.gaps | join('; ') }}\n"
                 "Topic: {{ initial_input.topic }}"
                 "{% else %}Research this topic with web_search, then write a "
                 "short sourced findings report: {{ initial_input.topic }}"
                 "{% endif %}")},
            {"kind": "agent", "id": "extractor", "agent_id": ext_id,
             "input_template": (
                 "Extract the verified, sourced facts as a numbered list:\n\n"
                 "{{ nodes.researcher.text }}")},
            {"kind": "agent", "id": "judge", "agent_id": jud_id,
             "input_template": (
                 "Topic: {{ initial_input.topic }}\n\nCurrent facts:\n"
                 "{{ nodes.extractor.text }}\n\nReturn JSON only."),
             "response_format": {
                 "type": "object",
                 "required": ["verdict", "gaps", "confidence"],
                 "properties": {
                     "verdict": {"type": "string", "enum": ["accept", "revise"]},
                     "gaps": {"type": "array", "items": {"type": "string"}},
                     "confidence": {"type": "number"}}}},
            {"kind": "end", "id": "done",
             "output_template": "{{ nodes.extractor.text }}"},
        ]
        edges = [
            {"kind": "static", "from_node": "start", "to_node": "researcher"},
            {"kind": "static", "from_node": "researcher", "to_node": "extractor"},
            {"kind": "static", "from_node": "extractor", "to_node": "judge"},
            {"kind": "conditional", "from_node": "judge", "router": {
                "kind": "json_path",
                "branches": [
                    {"conditions": [{"path": "verdict", "op": "eq", "value": "accept"}],
                     "to_node": "done"},
                    {"conditions": [{"path": "verdict", "op": "eq", "value": "revise"}],
                     "to_node": "researcher"}],
                "default_to": "done"}},
        ]
        pc.run("create", "-f", manifest(tmp_path, "graph", "graph", {
            "id": gid,
            "description": "Research a topic, distil sourced facts, judge until accepted.",
            "max_iterations": 12, "nodes": nodes, "edges": edges,
        }))

        # --- Local workspace --------------------------------------------
        pc.run("create", "-f", manifest(tmp_path, "wp", "workspace_provider", {
            "id": wp, "provider": "local",
            "config": {"kind": "local", "root_path": str(tmp_path)},
        }))
        pc.run("create", "-f", manifest(tmp_path, "tpl", "workspace_template", {
            "id": tpl, "description": "research cli", "provider_id": wp,
            "backend": {"kind": "local"},
        }))
        wid = pc.run(
            "create", "workspace", "--set", f"template_id={tpl}",
        ).stdout.split("/")[1].split()[0]

        # --- Run the graph with the topic as graph_input ----------------
        run = pc.run(
            "session", "run", wid, "--graph", gid,
            "--graph-input", json.dumps({"topic": _TOPIC}), "--timeout", "180",
        )
        assert "ended: completed" in run.stdout, run.stdout

        sid = None
        for line in run.stdout.splitlines():
            if line.startswith("session/") and "started" in line:
                sid = line.split("/", 1)[1].split()[0]
                break
        assert sid, f"could not parse session id:\n{run.stdout}"

        # --- The graph completed (not failed / max_iterations) ----------
        state = json.loads(_files_get(pc, wid, f".state/graphs/{sid}/state.json"))
        assert state["ended_reason"] == "completed", state
        nstates = state["node_states"]
        for nid in ("researcher", "extractor", "judge", "done"):
            assert nstates[nid]["status"] == "ended", (nid, nstates[nid])
            assert not nstates[nid]["error"], (nid, nstates[nid])

        # --- The loop iterated then converged ---------------------------
        # As the first node after begin, the researcher runs at iteration 1 on
        # a single linear pass; its last_run_iteration advancing past 1 is the
        # ONLY way the conditional back-edge re-dispatched it for a 2nd pass.
        researcher_iter = nstates["researcher"]["last_run_iteration"]
        assert researcher_iter > 1, (
            f"researcher ran only once (last_run_iteration={researcher_iter}); "
            "the revise back-edge did not re-dispatch it"
        )

        # --- The judge produced VALID structured JSON (revise then accept) -
        judge_blob = "\n".join(
            _files_get(pc, wid, p) for p in _node_message_logs(pc, wid, sid, "judge")
        )
        verdicts: list[str] = []
        for line in judge_blob.splitlines():
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
                    verdicts.append(parsed["verdict"])
        assert verdicts, "no judge verdict JSON recovered from node output"
        assert verdicts[0] == "revise", verdicts
        assert verdicts[-1] == "accept", verdicts

        # --- The researcher hit the REAL web ----------------------------
        res_blob = "\n".join(
            _files_get(pc, wid, p)
            for p in _node_message_logs(pc, wid, sid, "researcher")
        )
        assert "web__web_search" in res_blob, (
            "researcher never called the real web_search tool"
        )
        assert '"tool_result"' in res_blob, (
            "no web_search tool result round-tripped; the live DuckDuckGo "
            "backend did not return"
        )

        # --- The converged output flowed THROUGH the back-edge ----------
        # The graph session's final output (the end node's rendered extractor
        # facts) must carry the revised-pass marker, proving the accepted
        # result came from the second researcher pass, plus sourced content.
        report = _files_get(pc, wid, f".state/sessions/{sid}/messages.jsonl")
        assert _REVISED_MARKER in report, (
            "the converged final output does not carry the revised-pass "
            "marker; the back-edge did not feed a second researcher pass"
        )
        assert "source" in report.lower(), (
            "the final fact list carries no sourced content"
        )
    finally:
        for res, ident in cleanup:
            pc.run("delete", res, ident, check=False)


def _node_message_logs(pc: Primectl, wid: str, sid: str, node_id: str) -> list[str]:
    """Find every messages.jsonl under a graph node's state dir.

    The graph node history lives under
    ``.state/graphs/<sid>/nodes/<node_id>/`` (possibly in a per-run subdir),
    enumerated with the workspace file ``ls -R`` (recursive) verb the doc
    shows. Each listed row carries a workspace-relative ``path``.
    """
    root = f".state/graphs/{sid}/nodes/{node_id}"
    res = pc.run("workspace", "files", "ls", wid, root, "-R", "-o", "json",
                 check=False)
    if res.returncode != 0:
        return []
    try:
        rows = json.loads(res.stdout)
    except json.JSONDecodeError:
        return []
    rows = rows if isinstance(rows, list) else rows.get("items", [])
    return [
        r["path"] for r in rows
        if r.get("path", "").rsplit("/", 1)[-1] == "messages.jsonl"
        and r.get("kind") != "dir"
    ]
