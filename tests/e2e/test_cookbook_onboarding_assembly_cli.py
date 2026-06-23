r"""Cookbook recipe (CLI path): new-customer onboarding assembly, via primectl.

The ``primectl``-driven sibling of ``test_cookbook_onboarding_assembly``. Every
setup step is the exact ``primectl`` command the rewritten doc shows:

  * ``create -f`` the scripted LLM provider and the four agents (kyc, provision,
    region, coordinator);
  * ``create -f`` the three reusable child-graph manifests + the parent assembly
    manifest (sequential ``kind: graph`` subgraph nodes + a ``fan_out:
    broadcast`` OVER a subgraph + ``fan_in``), plus a deliberately-failing child
    and a fail-parent to prove a failing child fails the parent;
  * ``create -f`` / ``--set`` the local workspace; and
  * ``session run --agent`` the coordinator (which calls
    ``workspace_ext__invoke_graph`` on the parent), then ``workspace files get``
    to read the rolled-up transcript + the failing-graph state back.

The success outcome asserted is the API test's: every child subgraph's output
propagates into the parent end template (KYC + PROV), broadcast-over-subgraph
runs N isolated region instances (``__region[i]`` run dirs, never one shared
``__region``), the coordinator's ``invoke_graph`` tool result carries the
rolled-up summary, and a failing child fails the parent (subgraph node marked
``failed``).

All agents are scripted via the shared in-process ``mock_llm`` (deterministic
Rules), so the whole assembly is reproducible. Not capability-gated.

Recipe: primerhq.github.io/docs_source/cookbook/onboarding-assembly.md

Run with:
    PRIMER_RUN_E2E=1 uv run pytest \
        tests/e2e/test_cookbook_onboarding_assembly_cli.py -n0 -q
"""
from __future__ import annotations

import json

from tests._support.mock_llm import Rule
from tests._support.primectl_driver import Primectl, manifest, mint_token
from tests._support.smk import smk


_REGION_COUNT = 3
_KYC_LINE = "KYC VERIFIED"
_PROVISION_LINE = "ACCOUNT PROVISIONED"
_REGION_LINE = "REGION READY"


def _files_get(pc: Primectl, wid: str, rel: str) -> str:
    return pc.run("workspace", "files", "get", wid, rel, "--content").stdout


def _created_id(stdout: str) -> str:
    """`create -f` echoes `<name>/<server-id> created`; return the server id."""
    return stdout.split("/", 1)[1].split()[0]


def _child_graph(pc, tmp_path, *, name, gid, agent_id, end_template):
    nodes = [
        {"kind": "begin", "id": "begin"},
        {"kind": "agent", "id": "work", "agent_id": agent_id,
         "input_template": "{{ initial_input }}"},
        {"kind": "end", "id": "end", "output_template": end_template},
    ]
    edges = [
        {"kind": "static", "from_node": "begin", "to_node": "work"},
        {"kind": "static", "from_node": "work", "to_node": "end"},
    ]
    return pc.run("create", "-f", manifest(tmp_path, name, "graph", {
        "id": gid, "description": f"{name} child graph.",
        "max_iterations": 10, "nodes": nodes, "edges": edges,
    }))


@smk("SMK-COOKBOOK-CLI-10")
def test_onboarding_assembly_cli(base_url, mock_llm, unique_suffix, tmp_path):
    registry, mock_base_url = mock_llm
    sfx = unique_suffix
    pc = Primectl(base_url, mint_token(base_url, name=f"cli-onb-{sfx}"))

    pid = f"p-onb-cli-{sfx}"
    kyc_aid = f"kyc-agent-cli-{sfx}"
    prov_aid = f"prov-agent-cli-{sfx}"
    rgn_aid = f"region-agent-cli-{sfx}"
    fail_aid = f"fail-agent-cli-{sfx}"
    crd_aid = f"onboarding-coordinator-cli-{sfx}"
    kyc_gid = f"kyc-check-cli-{sfx}"
    prov_gid = f"provision-account-cli-{sfx}"
    rgn_gid = f"provision-region-cli-{sfx}"
    fail_gid = f"fail-child-cli-{sfx}"
    parent_gid = f"onboarding-assembly-cli-{sfx}"
    fail_parent_gid = f"fail-parent-cli-{sfx}"
    wp = f"wp-onb-cli-{sfx}"
    tpl = f"tpl-onb-cli-{sfx}"

    kyc_scn = f"scripted:onb-kyc-cli-{sfx}"
    prov_scn = f"scripted:onb-prov-cli-{sfx}"
    rgn_scn = f"scripted:onb-region-cli-{sfx}"
    fail_scn = f"scripted:onb-fail-cli-{sfx}"
    crd_scn = f"scripted:onb-coord-cli-{sfx}"
    registry.register(kyc_scn, [Rule(emit_text=_KYC_LINE)])
    registry.register(prov_scn, [Rule(emit_text=_PROVISION_LINE)])
    registry.register(rgn_scn, [Rule(emit_text=_REGION_LINE)])
    registry.register(fail_scn, [Rule(emit_text="OK")])
    # The coordinator drives the parent assembly and echoes its result.
    registry.register(crd_scn, [
        Rule(when_tool_result=False, emit_tool="workspace_ext__invoke_graph",
             emit_args={"graph_id": parent_gid, "input": "Acme Corp"}),
        Rule(when_tool_result=True, emit_text="ONBOARDING COMPLETE"),
    ])

    cleanup = [
        ("graph", fail_parent_gid), ("graph", parent_gid), ("graph", fail_gid),
        ("graph", rgn_gid), ("graph", prov_gid), ("graph", kyc_gid),
        ("agent", crd_aid), ("agent", fail_aid), ("agent", rgn_aid),
        ("agent", prov_aid), ("agent", kyc_aid), ("llm_provider", pid),
    ]
    try:
        # --- The scripted LLM provider (all five scenarios) --------------
        pc.run("create", "-f", manifest(tmp_path, "llm", "llm_provider", {
            "id": pid, "provider": "openchat",
            "models": [{"name": s, "context_length": 8192}
                       for s in (kyc_scn, prov_scn, rgn_scn, fail_scn, crd_scn)],
            "config": {"url": mock_base_url, "flavor": "lmstudio"},
            "limits": {"max_concurrency": 4},
        }))

        # --- The five agents (create -f) ---------------------------------
        for aid, scn, prompt in (
            (kyc_aid, kyc_scn, "Verify the customer's identity."),
            (prov_aid, prov_scn, "Provision the customer's account."),
            (rgn_aid, rgn_scn, "Provision a regional footprint."),
            (fail_aid, fail_scn, "Reply OK."),
        ):
            pc.run("create", "-f", manifest(tmp_path, aid, "agent", {
                "id": aid, "description": prompt,
                "model": {"provider_id": pid, "model_name": scn},
                "tools": [], "system_prompt": [prompt],
            }))
        pc.run("create", "-f", manifest(tmp_path, "coord", "agent", {
            "id": crd_aid, "description": "Kicks off the onboarding assembly.",
            "model": {"provider_id": pid, "model_name": crd_scn},
            "tools": ["workspace_ext__invoke_graph"],
            "system_prompt": [
                "When asked to onboard a customer, call invoke_graph with the "
                "onboarding-assembly graph and the customer name, then report "
                "the returned output."
            ],
        }))

        # --- The three reusable child graphs (begin -> agent -> end) -----
        _child_graph(pc, tmp_path, name="kyc", gid=kyc_gid,
                     agent_id=kyc_aid, end_template="{{ nodes.work.text }}")
        _child_graph(pc, tmp_path, name="prov", gid=prov_gid,
                     agent_id=prov_aid, end_template="{{ nodes.work.text }}")
        _child_graph(pc, tmp_path, name="region", gid=rgn_gid,
                     agent_id=rgn_aid, end_template="{{ nodes.work.text }}")
        # The deliberately-failing child: a missing-node reference in `end`.
        _child_graph(pc, tmp_path, name="failchild", gid=fail_gid,
                     agent_id=fail_aid,
                     end_template="{{ nodes.does_not_exist.text }}")

        # --- The parent assembly: seq subgraphs + broadcast-over-subgraph -
        parent_nodes = [
            {"kind": "begin", "id": "begin"},
            {"kind": "graph", "id": "kyc", "graph_id": kyc_gid,
             "input_template": "Customer: {{ initial_input }}"},
            {"kind": "graph", "id": "provision", "graph_id": prov_gid,
             "input_template": "Provision for {{ initial_input }}"},
            {"kind": "fan_out", "id": "regions", "specs": [{
                "kind": "broadcast", "target_node_id": "region",
                "count": _REGION_COUNT}]},
            {"kind": "graph", "id": "region", "graph_id": rgn_gid,
             "input_template": "Provision region #{{ fanout_index }}"},
            {"kind": "fan_in", "id": "rollup", "aggregate_template": (
                "{% for r in nodes.region %}region #{{ loop.index0 }}: "
                "{{ r.text }}\n{% endfor %}")},
            {"kind": "end", "id": "end", "output_template": (
                "KYC={{ nodes.kyc.text }} | PROV={{ nodes.provision.text }}\n"
                "{{ nodes.rollup.text }}")},
        ]
        parent_edges = [
            {"kind": "static", "from_node": "begin", "to_node": "kyc"},
            {"kind": "static", "from_node": "kyc", "to_node": "provision"},
            {"kind": "static", "from_node": "provision", "to_node": "regions"},
            {"kind": "static", "from_node": "region", "to_node": "rollup"},
            {"kind": "static", "from_node": "rollup", "to_node": "end"},
        ]
        pc.run("create", "-f", manifest(tmp_path, "parent", "graph", {
            "id": parent_gid,
            "description": "Compose KYC + account provisioning, then a regional footprint.",
            "max_iterations": 30, "nodes": parent_nodes, "edges": parent_edges,
        }))

        # --- A parent whose only subgraph is the failing child -----------
        pc.run("create", "-f", manifest(tmp_path, "failparent", "graph", {
            "id": fail_parent_gid,
            "description": "Parent of a failing child.",
            "max_iterations": 10,
            "nodes": [
                {"kind": "begin", "id": "begin"},
                {"kind": "graph", "id": "badchild", "graph_id": fail_gid,
                 "input_template": "{{ initial_input }}"},
                {"kind": "end", "id": "end",
                 "output_template": "{{ nodes.badchild.text }}"},
            ],
            "edges": [
                {"kind": "static", "from_node": "begin", "to_node": "badchild"},
                {"kind": "static", "from_node": "badchild", "to_node": "end"},
            ],
        }))

        # --- Local workspace --------------------------------------------
        pc.run("create", "-f", manifest(tmp_path, "wp", "workspace_provider", {
            "id": wp, "provider": "local",
            "config": {"kind": "local", "root_path": str(tmp_path)},
        }))
        pc.run("create", "-f", manifest(tmp_path, "tpl", "workspace_template", {
            "id": tpl, "description": "onb cli", "provider_id": wp,
            "backend": {"kind": "local"},
        }))
        wid = pc.run(
            "create", "workspace", "--set", f"template_id={tpl}",
        ).stdout.split("/")[1].split()[0]

        # =================================================================
        # (A) The coordinator drives the WHOLE assembly via invoke_graph.
        # =================================================================
        run = pc.run(
            "session", "run", wid, "--agent", crd_aid,
            "-i", "Onboard the new customer.", "--timeout", "180",
        )
        assert "ended: completed" in run.stdout, run.stdout
        sid = None
        for line in run.stdout.splitlines():
            if line.startswith("session/") and "started" in line:
                sid = line.split("/", 1)[1].split()[0]
                break
        assert sid, f"could not parse session id:\n{run.stdout}"

        transcript = _files_get(pc, wid, f".state/sessions/{sid}/messages.jsonl")

        # (1) invoke_graph returned the rolled-up summary -- only renders if
        # EVERY child subgraph's output propagated to the parent end template.
        assert "workspace_ext__invoke_graph" in transcript, transcript
        assert f"KYC={_KYC_LINE}" in transcript, (
            f"kyc subgraph output not propagated: {transcript!r}"
        )
        assert f"PROV={_PROVISION_LINE}" in transcript, (
            f"provision subgraph output not propagated: {transcript!r}"
        )
        # (2) broadcast-over-subgraph: every region instance produced its line.
        for i in range(_REGION_COUNT):
            assert f"region #{i}: {_REGION_LINE}" in transcript, (
                f"region[{i}] subgraph output missing from the rollup: "
                f"{transcript!r}"
            )

        # (3) the invoke_graph parent + each child run nested under the session
        # as DISTINCT on-disk dirs, including one isolated __region[i] per
        # instance (never one shared __region) -- the Bug3 guard. Enumerated
        # via the recursive workspace file ls the doc shows.
        graph_dirs = _graph_dir_names(pc, wid)
        invoke_dirs = [d for d in graph_dirs if d.startswith(f"{sid}__invoke_")]
        region_dirs = {
            d for d in invoke_dirs
            if any(d.endswith(f"__region[{i}]") for i in range(_REGION_COUNT))
        }
        assert len(region_dirs) == _REGION_COUNT, (
            f"expected {_REGION_COUNT} isolated region subgraph run dirs "
            f"(__region[i]); got {sorted(region_dirs)} of all {invoke_dirs}"
        )
        assert not any(d.endswith("__region") for d in invoke_dirs), (
            f"a shared un-indexed __region run dir leaked (Bug3): {invoke_dirs}"
        )

        # =================================================================
        # (B) A failing child FAILS the parent (not silent success). Run the
        # fail-parent directly as a graph session and read its state.
        # =================================================================
        frun = pc.run(
            "session", "run", wid, "--graph", fail_parent_gid,
            "--graph-input", json.dumps("x"), "--timeout", "90",
        )
        fsid = None
        for line in frun.stdout.splitlines():
            if line.startswith("session/") and "started" in line:
                fsid = line.split("/", 1)[1].split()[0]
                break
        assert fsid, f"could not parse fail-parent session id:\n{frun.stdout}"

        fstate = json.loads(_files_get(pc, wid, f".state/graphs/{fsid}/state.json"))
        assert fstate["ended_reason"] == "failed", (
            f"a failing child must fail the parent, not complete it: {fstate}"
        )
        badchild = fstate["node_states"]["badchild"]
        assert badchild["status"] == "failed", (
            f"the failing subgraph node should be marked failed: {badchild}"
        )
        assert badchild.get("error"), (
            f"the failed subgraph node should carry a non-empty error: {badchild}"
        )
    finally:
        for res, ident in cleanup:
            pc.run("delete", res, ident, check=False)


def _graph_dir_names(pc: Primectl, wid: str) -> list[str]:
    """The immediate child dir names under .state/graphs (the run ids).

    Listed with the workspace file ``ls`` verb the doc shows; each row's
    ``path`` is workspace-relative, so we take its last segment.
    """
    res = pc.run("workspace", "files", "ls", wid, ".state/graphs", "-o", "json",
                 check=False)
    if res.returncode != 0:
        return []
    try:
        rows = json.loads(res.stdout)
    except json.JSONDecodeError:
        return []
    rows = rows if isinstance(rows, list) else rows.get("items", [])
    return [r.get("path", "").rstrip("/").rsplit("/", 1)[-1] for r in rows]
