"""Cookbook recipe #12 regression: new-customer onboarding assembly.

A coordinator agent kicks off a parent onboarding graph that COMPOSES reusable
child graphs as ``kind: graph`` (subgraph) nodes -- ``kyc-check`` then
``provision-account`` run sequentially -- and then broadcasts a third child
graph (``provision-region``) across N regions with a ``fan_out: broadcast`` OVER
a subgraph target. A ``fan_in`` rolls the regions up and the ``end`` node weaves
every child's output into one onboarding summary. The coordinator runs the whole
assembly inside its own session via ``workspace_ext__invoke_graph``.

Recipe: primerhq.github.io/docs_source/cookbook/onboarding-assembly.md

This is the HIGH-RISK composition recipe: it exercises the TWO distinct subgraph
code paths (the ``kind: graph`` NODE path in ``primer/graph/base.py``'s
``_stream_subgraph_node`` and the ``workspace_ext__invoke_graph`` TOOL path in
``primer/graph/invoke_graph.py``) and pins the four mechanics that historically
broke and are now fixed:

  * **subgraph node output PROPAGATES to the parent** -- a child graph's End
    output is captured (NOT dropped to an empty string): ``nodes.kyc`` and
    ``nodes.provision`` text reaches the parent's ``end`` template. This guards
    the child-end-output-drop regression (fix commit 4fda70ff for the node path;
    the same two-channel capture in ``invoke_graph.py``).
  * **a FAILING child FAILS the parent** (not silent success): a child graph
    whose ``end`` template references a missing node ends ``failed``, and the
    parent subgraph node ends ``failed`` with a ``_SubgraphFailed`` error rather
    than advancing past the broken child.
  * **broadcast OVER a subgraph isolates per-instance state**: the ``region``
    fan-out spawns ``region[0..N-1]``, each its OWN nested child-graph run dir
    (``__region[i]``, never one shared ``__region``) -- the checkpoint-sharing
    regression (Bug3).
  * **``invoke_graph`` returns the child graph result to the calling agent**:
    the coordinator's ``workspace_ext__invoke_graph`` tool_result carries the
    rolled-up onboarding summary text.

All agents are scripted with the deterministic mock LLM (child graphs emit fixed
KYC/provision/region lines; the coordinator emits the invoke_graph call then
echoes the result), so the whole assembly is reproducible on any model.

Run with:
    PRIMER_RUN_E2E=1 uv run pytest tests/e2e/test_cookbook_onboarding_assembly.py -n0 -q
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
    start_agent_session,
    wait_terminal,
)
from tests._support.smk import smk

pytestmark = [pytest.mark.asyncio]


_REGION_COUNT = 3
_KYC_LINE = "KYC VERIFIED"
_PROVISION_LINE = "ACCOUNT PROVISIONED"
_REGION_LINE = "REGION READY"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _child_graph_nodes(agent_id: str, *, end_template: str) -> tuple[list, list]:
    """A reusable child graph: begin -> agent (the work) -> end."""
    nodes = [
        {"kind": "begin", "id": "begin"},
        {
            "kind": "agent", "id": "work", "agent_id": agent_id,
            "input_template": "{{ initial_input }}",
        },
        {"kind": "end", "id": "end", "output_template": end_template},
    ]
    edges = [
        {"kind": "static", "from_node": "begin", "to_node": "work"},
        {"kind": "static", "from_node": "work", "to_node": "end"},
    ]
    return nodes, edges


def _graph_state(tmp_path: Path, wid: str, sid: str) -> dict:
    p = tmp_path / wid / ".state" / "graphs" / sid / "state.json"
    assert p.exists(), f"graph state.json missing at {p}"
    return json.loads(p.read_text(encoding="utf-8"))


def _session_transcript(tmp_path: Path, wid: str, sid: str) -> str:
    p = tmp_path / wid / ".state" / "sessions" / sid / "messages.jsonl"
    assert p.exists(), f"session transcript missing at {p}"
    return p.read_text(encoding="utf-8")


def _invoke_graph_gsid_dirs(tmp_path: Path, wid: str, sid: str) -> list[str]:
    """The graph dirs invoke_graph nests under the agent session: the parent
    run is ``<sid>__invoke_<tcid>`` and each child subgraph run nests further
    (``...__kyc``, ``...__region[i]``)."""
    base = tmp_path / wid / ".state" / "graphs"
    if not base.exists():
        return []
    return sorted(p.name for p in base.iterdir() if p.name.startswith(f"{sid}__invoke_"))


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@smk("SMK-COOKBOOK-12")
async def test_onboarding_assembly_composes_subgraphs(
    authed_client, mock_llm, unique_suffix, tmp_path,
):
    registry, base_url = mock_llm
    sfx = f"onb-{unique_suffix}"
    cleanup: list[str] = []
    try:
        # --- Three reusable child-graph agents (scripted, deterministic). ---
        kyc_agent = await make_scripted_agent(
            authed_client, registry, base_url, suffix=f"kyc{unique_suffix}",
            scenario=f"scripted:kyc-{unique_suffix}",
            system_prompt=["Verify the customer's identity."],
            rules=[Rule(emit_text=_KYC_LINE)],
        )
        prov_agent = await make_scripted_agent(
            authed_client, registry, base_url, suffix=f"prv{unique_suffix}",
            scenario=f"scripted:prov-{unique_suffix}",
            system_prompt=["Provision the customer's account."],
            rules=[Rule(emit_text=_PROVISION_LINE)],
        )
        region_agent = await make_scripted_agent(
            authed_client, registry, base_url, suffix=f"rgn{unique_suffix}",
            scenario=f"scripted:region-{unique_suffix}",
            system_prompt=["Provision a regional footprint."],
            rules=[Rule(emit_text=_REGION_LINE)],
        )
        # A child whose end template references a missing node -> the child
        # ends `failed`; we use it to prove a failing child fails the parent.
        fail_agent = await make_scripted_agent(
            authed_client, registry, base_url, suffix=f"fail{unique_suffix}",
            scenario=f"scripted:fail-{unique_suffix}",
            system_prompt=["Reply OK."],
            rules=[Rule(emit_text="OK")],
        )

        # --- Child graphs. ---
        kyc_nodes, kyc_edges = _child_graph_nodes(
            kyc_agent["agent_id"], end_template="{{ nodes.work.text }}",
        )
        kyc_gid = await make_graph(
            authed_client, suffix=f"kyc{unique_suffix}",
            nodes=kyc_nodes, edges=kyc_edges, max_iterations=10,
        )
        prov_nodes, prov_edges = _child_graph_nodes(
            prov_agent["agent_id"], end_template="{{ nodes.work.text }}",
        )
        prov_gid = await make_graph(
            authed_client, suffix=f"prv{unique_suffix}",
            nodes=prov_nodes, edges=prov_edges, max_iterations=10,
        )
        region_nodes, region_edges = _child_graph_nodes(
            region_agent["agent_id"], end_template="{{ nodes.work.text }}",
        )
        region_gid = await make_graph(
            authed_client, suffix=f"rgn{unique_suffix}",
            nodes=region_nodes, edges=region_edges, max_iterations=10,
        )
        # The deliberately-failing child: a missing-node reference in `end`.
        fail_nodes, fail_edges = _child_graph_nodes(
            fail_agent["agent_id"],
            end_template="{{ nodes.does_not_exist.text }}",
        )
        fail_gid = await make_graph(
            authed_client, suffix=f"fail{unique_suffix}",
            nodes=fail_nodes, edges=fail_edges, max_iterations=10,
        )

        # --- Parent assembly: seq subgraphs + broadcast-over-subgraph. ---
        parent_nodes = [
            {"kind": "begin", "id": "begin"},
            {
                "kind": "graph", "id": "kyc", "graph_id": kyc_gid,
                "input_template": "Customer: {{ initial_input }}",
            },
            {
                "kind": "graph", "id": "provision", "graph_id": prov_gid,
                "input_template": "Provision for {{ initial_input }}",
            },
            {
                "kind": "fan_out", "id": "regions",
                "specs": [{
                    "kind": "broadcast",
                    "target_node_id": "region",
                    "count": _REGION_COUNT,
                }],
            },
            {
                "kind": "graph", "id": "region", "graph_id": region_gid,
                "input_template": "Provision region #{{ fanout_index }}",
            },
            {
                "kind": "fan_in", "id": "rollup",
                "aggregate_template": (
                    "{% for r in nodes.region %}"
                    "region #{{ loop.index0 }}: {{ r.text }}\n"
                    "{% endfor %}"
                ),
            },
            {
                "kind": "end", "id": "end",
                "output_template": (
                    "KYC={{ nodes.kyc.text }} | "
                    "PROV={{ nodes.provision.text }}\n"
                    "{{ nodes.rollup.text }}"
                ),
            },
        ]
        parent_edges = [
            {"kind": "static", "from_node": "begin", "to_node": "kyc"},
            {"kind": "static", "from_node": "kyc", "to_node": "provision"},
            {"kind": "static", "from_node": "provision", "to_node": "regions"},
            {"kind": "static", "from_node": "region", "to_node": "rollup"},
            {"kind": "static", "from_node": "rollup", "to_node": "end"},
        ]
        parent_gid = await make_graph(
            authed_client, suffix=f"onb{unique_suffix}",
            nodes=parent_nodes, edges=parent_edges, max_iterations=30,
        )

        # --- A parent whose only subgraph is the failing child. ---
        fail_parent_nodes = [
            {"kind": "begin", "id": "begin"},
            {
                "kind": "graph", "id": "badchild", "graph_id": fail_gid,
                "input_template": "{{ initial_input }}",
            },
            {"kind": "end", "id": "end",
             "output_template": "{{ nodes.badchild.text }}"},
        ]
        fail_parent_edges = [
            {"kind": "static", "from_node": "begin", "to_node": "badchild"},
            {"kind": "static", "from_node": "badchild", "to_node": "end"},
        ]
        fail_parent_gid = await make_graph(
            authed_client, suffix=f"flp{unique_suffix}",
            nodes=fail_parent_nodes, edges=fail_parent_edges, max_iterations=10,
        )

        # --- Coordinator agent: calls invoke_graph(parent), echoes result. ---
        coordinator = await make_scripted_agent(
            authed_client, registry, base_url, suffix=f"crd{unique_suffix}",
            scenario=f"scripted:coord-{unique_suffix}",
            tools=["workspace_ext__invoke_graph"],
            system_prompt=["You are the onboarding coordinator."],
            rules=[
                # First turn (no tool result yet): kick off the assembly graph.
                Rule(
                    when_tool_result=False,
                    emit_tool="workspace_ext__invoke_graph",
                    emit_args={"graph_id": parent_gid, "input": "Acme Corp"},
                ),
                # After the graph returns: echo its rolled-up output verbatim.
                Rule(when_tool_result=True, emit_text="ONBOARDING COMPLETE"),
            ],
        )

        wid = await make_local_workspace(
            authed_client, suffix=sfx, root=tmp_path,
        )

        # =================================================================
        # (A) Coordinator drives the WHOLE assembly via invoke_graph. This
        # exercises the invoke_graph TOOL path; the parent assembly inside it
        # exercises the subgraph NODE path + broadcast-over-subgraph.
        # =================================================================
        sid = await start_agent_session(
            authed_client, workspace_id=wid, agent_id=coordinator["agent_id"],
            instructions="Onboard the new customer.",
        )
        final = await wait_terminal(authed_client, sid, timeout_s=180)
        assert final.get("status") == "ended", final
        assert final.get("ended_reason") == "completed", final

        transcript = _session_transcript(tmp_path, wid, sid)

        # (1) invoke_graph returned the child graph result to the coordinator:
        # the tool_result carries the fully-rolled-up onboarding summary, which
        # only renders if EVERY child subgraph's output propagated.
        assert "workspace_ext__invoke_graph" in transcript, transcript
        # KYC + provision subgraph node output reached the parent end template.
        assert f"KYC={_KYC_LINE}" in transcript, (
            f"kyc subgraph output not propagated into the invoke_graph result: "
            f"{transcript!r}"
        )
        assert f"PROV={_PROVISION_LINE}" in transcript, (
            f"provision subgraph output not propagated: {transcript!r}"
        )
        # (2) broadcast-over-subgraph: all N region instances ran and each
        # produced its own line in the rollup (per-instance, index-aligned).
        for i in range(_REGION_COUNT):
            assert f"region #{i}: {_REGION_LINE}" in transcript, (
                f"region[{i}] subgraph output missing from the rollup -- "
                f"broadcast-over-subgraph did not isolate/propagate instance "
                f"{i}: {transcript!r}"
            )

        # (3) the invoke_graph parent run + each child subgraph run nested
        # under the coordinator session as DISTINCT on-disk dirs -- including
        # one dir PER region instance (__region[0..N-1]), never one shared
        # __region. This is the Bug3 (shared-checkpoint) guard.
        igsids = _invoke_graph_gsid_dirs(tmp_path, wid, sid)
        region_dirs = {d for d in igsids if d.endswith(tuple(
            f"__region[{i}]" for i in range(_REGION_COUNT)
        ))}
        assert len(region_dirs) == _REGION_COUNT, (
            f"expected {_REGION_COUNT} isolated region subgraph run dirs "
            f"(__region[i]); got region dirs {sorted(region_dirs)} out of "
            f"all invoke dirs {igsids}"
        )
        # No shared (un-indexed) __region dir leaked alongside the indexed ones.
        assert not any(d.endswith("__region") for d in igsids), (
            f"a shared un-indexed __region run dir leaked (Bug3 regression): "
            f"{igsids}"
        )

        # =================================================================
        # (B) A failing child FAILS the parent (not silent success). Run the
        # fail-parent graph directly as a graph session and assert it ends
        # failed with the subgraph node marked failed.
        # =================================================================
        from tests._support.runs import start_graph_session

        fsid = await start_graph_session(
            authed_client, workspace_id=wid, graph_id=fail_parent_gid,
            instructions="x",
        )
        ffinal = await wait_terminal(authed_client, fsid, timeout_s=90)
        assert ffinal.get("status") == "ended", ffinal

        fstate = _graph_state(tmp_path, wid, fsid)
        assert fstate["ended_reason"] == "failed", (
            f"a failing child must fail the parent graph, not complete it: "
            f"{fstate}"
        )
        badchild = fstate["node_states"]["badchild"]
        assert badchild["status"] == "failed", (
            f"the failing subgraph node should be marked failed: {badchild}"
        )
        assert badchild.get("error"), (
            f"the failed subgraph node should carry a non-empty error "
            f"(the _SubgraphFailed detail): {badchild}"
        )
    finally:
        for url in reversed(cleanup):
            await authed_client.delete(url)
