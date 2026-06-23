"""Cookbook recipe (CLI path): fan-out code review, driven by primectl.

The ``primectl``-driven sibling of ``test_cookbook_fanout_code_review``. Every
setup step is the exact ``primectl`` command the rewritten doc shows:

  * ``create -f`` the scripted LLM provider and two reviewer agents;
  * ``create -f`` the fan-out (``tee``) / fan-in graph manifest (the full
    node/edge spec, the way the doc leads with a CLI manifest);
  * ``create -f`` / ``--set`` the local workspace; and
  * ``session run --graph ... --graph-input`` the graph with the code payload,
    then ``workspace files get`` to read the on-disk graph state + the
    aggregated report back.

The success outcome asserted is the API test's: the graph ends ``completed``,
both reviewer node instances ran in the SAME superstep (the tee dispatches them
together), and the fan_in report carries both reviewers' distinctive findings.
That the run completes at all is itself proof the list-indexed tee accessor
(``nodes.review_bugs[0].text``) rendered: the wrong ``.text`` accessor would
end the run with ``template_error``.

The reviewers are scripted via the shared in-process ``mock_llm`` (deterministic
Rules); no real model or external dependency is needed, so this test is not
capability-gated.

Recipe: primerhq.github.io/docs_source/cookbook/fanout-code-review.md
"""
from __future__ import annotations

import json

from tests._support.mock_llm import Rule
from tests._support.primectl_driver import Primectl, manifest, mint_token
from tests._support.smk import smk


_BUGS_FINDING = "ZeroDivisionError when b is zero"
_STYLE_FINDING = "return the result directly and add a docstring"


@smk("SMK-COOKBOOK-CLI-03")
def test_fanout_code_review_cli(base_url, mock_llm, unique_suffix, tmp_path):
    registry, mock_base_url = mock_llm
    sfx = unique_suffix
    pc = Primectl(base_url, mint_token(base_url, name=f"cli-fan-{sfx}"))

    pid = f"p-fan-cli-{sfx}"
    bugs_id = f"reviewer-bugs-cli-{sfx}"
    style_id = f"reviewer-style-cli-{sfx}"
    gid = f"code-review-cli-{sfx}"
    wp = f"wp-fan-cli-{sfx}"
    tpl = f"tpl-fan-cli-{sfx}"

    # One scripted scenario per reviewer (each emits its one distinctive finding).
    bugs_scenario = f"scripted:rev-bugs-cli-{sfx}"
    style_scenario = f"scripted:rev-style-cli-{sfx}"
    registry.register(bugs_scenario, [Rule(emit_text=_BUGS_FINDING)])
    registry.register(style_scenario, [Rule(emit_text=_STYLE_FINDING)])

    cleanup = [("graph", gid), ("agent", bugs_id), ("agent", style_id),
               ("llm_provider", pid)]
    try:
        # --- The scripted LLM provider (lists both reviewer scenarios) ---
        pc.run("create", "-f", manifest(tmp_path, "llm", "llm_provider", {
            "id": pid, "provider": "openchat",
            "models": [
                {"name": bugs_scenario, "context_length": 8192},
                {"name": style_scenario, "context_length": 8192},
            ],
            "config": {"url": mock_base_url, "flavor": "lmstudio"},
            "limits": {"max_concurrency": 4},
        }))

        # --- The two reviewer agents (create -f) -------------------------
        pc.run("create", "-f", manifest(tmp_path, "bugs", "agent", {
            "id": bugs_id, "description": "Code reviewer.",
            "model": {"provider_id": pid, "model_name": bugs_scenario},
            "tools": [],
            "system_prompt": [
                "You review code for BUGS and correctness issues ONLY. List "
                "concrete bugs concisely; if none, say so."
            ],
        }))
        pc.run("create", "-f", manifest(tmp_path, "style", "agent", {
            "id": style_id, "description": "Code reviewer.",
            "model": {"provider_id": pid, "model_name": style_scenario},
            "tools": [],
            "system_prompt": [
                "You review code for STYLE and readability issues ONLY. List "
                "concrete style improvements concisely; if none, say so."
            ],
        }))

        # --- The fan-out (tee) / fan-in graph (create -f manifest) -------
        nodes = [
            {"kind": "begin", "id": "start", "input_schema": {
                "type": "object", "required": ["code"],
                "properties": {"code": {"type": "string"}}}},
            {"kind": "fan_out", "id": "split",
             "specs": [{"kind": "tee", "target_node_ids": ["review_bugs", "review_style"]}]},
            {"kind": "agent", "id": "review_bugs", "agent_id": bugs_id,
             "input_template": "Review this code for BUGS only:\n{{ initial_input.code }}"},
            {"kind": "agent", "id": "review_style", "agent_id": style_id,
             "input_template": "Review this code for STYLE only:\n{{ initial_input.code }}"},
            {"kind": "fan_in", "id": "combine", "aggregate_template": (
                "## Bugs\n{{ nodes.review_bugs[0].text }}\n\n"
                "## Style\n{{ nodes.review_style[0].text }}")},
            {"kind": "end", "id": "done", "output_template": "{{ nodes.combine.text }}"},
        ]
        edges = [
            {"kind": "static", "from_node": "start", "to_node": "split"},
            {"kind": "static", "from_node": "review_bugs", "to_node": "combine"},
            {"kind": "static", "from_node": "review_style", "to_node": "combine"},
            {"kind": "static", "from_node": "combine", "to_node": "done"},
        ]
        pc.run("create", "-f", manifest(tmp_path, "graph", "graph", {
            "id": gid,
            "description": "Fan-out code review: parallel reviewers, aggregated.",
            "max_iterations": 10, "nodes": nodes, "edges": edges,
        }))

        # --- Local workspace --------------------------------------------
        pc.run("create", "-f", manifest(tmp_path, "wp", "workspace_provider", {
            "id": wp, "provider": "local",
            "config": {"kind": "local", "root_path": str(tmp_path)},
        }))
        pc.run("create", "-f", manifest(tmp_path, "tpl", "workspace_template", {
            "id": tpl, "description": "fan cli", "provider_id": wp, "backend": {"kind": "local"},
        }))
        wid = pc.run("create", "workspace", "--set", f"template_id={tpl}").stdout.split("/")[1].split()[0]

        # --- Run the graph with the code payload as graph_input ----------
        code = {"code": "def divide(a, b):\n    return a / b"}
        run = pc.run(
            "session", "run", wid, "--graph", gid,
            "--graph-input", json.dumps(code), "--timeout", "120",
        )
        assert "ended: completed" in run.stdout, run.stdout

        sid = None
        for line in run.stdout.splitlines():
            if line.startswith("session/") and "started" in line:
                sid = line.split("/", 1)[1].split()[0]
                break
        assert sid, f"could not parse session id:\n{run.stdout}"

        # --- Read the on-disk graph state via the file verb the doc shows
        state = json.loads(pc.run(
            "workspace", "files", "get", wid,
            f".state/graphs/{sid}/state.json", "--content",
        ).stdout)
        assert state["ended_reason"] == "completed", state

        # Both reviewers ran in the SAME superstep (tee dispatches together).
        ns = state["node_states"]
        assert ns["review_bugs"]["status"] == "ended", ns
        assert ns["review_style"]["status"] == "ended", ns
        assert ns["review_bugs"]["last_run_iteration"] == ns["review_style"]["last_run_iteration"], (
            f"reviewers ran in different supersteps: "
            f"bugs@{ns['review_bugs']['last_run_iteration']} "
            f"style@{ns['review_style']['last_run_iteration']}"
        )

        # The aggregated fan_in report carries BOTH findings.
        report = pc.run(
            "workspace", "files", "get", wid,
            f".state/sessions/{sid}/messages.jsonl", "--content",
        ).stdout
        assert "## Bugs" in report and "## Style" in report, report
        assert _BUGS_FINDING in report, report
        assert _STYLE_FINDING in report, report
    finally:
        for res, ident in cleanup:
            pc.run("delete", res, ident, check=False)
