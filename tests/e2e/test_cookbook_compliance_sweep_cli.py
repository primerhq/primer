r"""Cookbook recipe (CLI path): overnight compliance sweep, driven by primectl.

The ``primectl``-driven sibling of ``test_cookbook_compliance_sweep``. Every
setup step is the exact ``primectl`` command the rewritten doc shows:

  * ``create -f`` the scripted scope-lister LLM provider + agent;
  * ``create -f`` the ``fan_out: map`` / ``on_failure: collect`` / ``fan_in``
    graph manifest (the way the doc leads the graph step with a CLI manifest /
    the editor's Import-spec paste);
  * ``create -f`` / ``--set`` the local workspace;
  * ``create -f`` the scheduled trigger and ``call trigger subscriptions`` to
    bind a ``graph_fresh_session`` subscription; and
  * ``call trigger fire-now`` to stand in for the 02:00 cron, then ``get
    session`` to poll the fired graph to terminal and ``workspace files get``
    to read the on-disk state + report back.

The success outcome asserted is the API test's: the ``graph_fresh_session``-
fired graph runs to terminal ``completed`` even though one branch fails, ``map``
dispatches one isolated ``audit[i]`` per service, exactly the unreachable branch
is ``collect``ed as failed, and the ``fan_in`` report ships every service.

The audit leg is a deterministic ``tool_call`` map target (``misc__calculate``)
so it needs no model; only the scope-lister uses the LLM, scripted via the
shared in-process ``mock_llm``. Not capability-gated.

Recipe: primerhq.github.io/docs_source/cookbook/compliance-sweep.md

Run with:
    PRIMER_RUN_E2E=1 uv run pytest \
        tests/e2e/test_cookbook_compliance_sweep_cli.py -n0 -q
"""
from __future__ import annotations

import json
import time

import yaml

from tests._support.mock_llm import Rule
from tests._support.primectl_driver import Primectl, manifest, mint_token
from tests._support.smk import smk


# The watchlist. `payments-legacy` is the unreachable one: `1 / 0` errors, the
# tool output is the error string (not {"expression","result"}), the node's
# output_schema rejects it -> tool_output_invalid -> the branch is collected.
_SERVICES = [
    {"name": "billing-api", "expr": "90 + 5"},
    {"name": "auth-svc", "expr": "80 + 8"},
    {"name": "payments-legacy", "expr": "1 / 0"},  # unreachable -> fails
    {"name": "search-svc", "expr": "70 + 9"},
]
_N = len(_SERVICES)
_FAIL_INDEX = 2  # payments-legacy


def _files_get(pc: Primectl, wid: str, rel: str) -> str:
    return pc.run("workspace", "files", "get", wid, rel, "--content").stdout


def _poll_session(pc: Primectl, sid: str, *, timeout_s: float = 120.0) -> dict:
    """Poll GET /v1/sessions/<id> via `primectl get session` until terminal.

    `get session <id> -o json -r` prints the bare session body (the `-r/--raw
    -output` flag the prior batches recorded as required for the un-enveloped
    object).
    """
    deadline = time.monotonic() + timeout_s
    while True:
        row = json.loads(
            pc.run("get", "session", sid, "-o", "json", "-r").stdout
        )
        if row.get("status") == "ended":
            return row
        if time.monotonic() > deadline:
            raise AssertionError(f"session {sid} did not end within {timeout_s}s: {row}")
        time.sleep(2.0)


@smk("SMK-COOKBOOK-CLI-09")
def test_compliance_sweep_cli(base_url, mock_llm, unique_suffix, tmp_path):
    registry, mock_base_url = mock_llm
    sfx = unique_suffix
    pc = Primectl(base_url, mint_token(base_url, name=f"cli-cmpl-{sfx}"))

    pid = f"p-cmpl-cli-{sfx}"
    scope_id = f"scope-lister-cli-{sfx}"
    gid = f"compliance-sweep-cli-{sfx}"
    wp = f"wp-cmpl-cli-{sfx}"
    tpl = f"tpl-cmpl-cli-{sfx}"
    slug = f"nightly-compliance-cli-{sfx}"

    scenario = f"scripted:cmpl-cli-{sfx}"
    registry.register(scenario, [Rule(emit_text=json.dumps({"services": _SERVICES}))])

    trigger_id: str | None = None
    cleanup = [("graph", gid), ("agent", scope_id), ("llm_provider", pid)]
    try:
        # --- The scripted LLM provider + scope-lister agent --------------
        pc.run("create", "-f", manifest(tmp_path, "llm", "llm_provider", {
            "id": pid, "provider": "openchat",
            "models": [{"name": scenario, "context_length": 8192}],
            "config": {"url": mock_base_url, "flavor": "lmstudio"},
            "limits": {"max_concurrency": 4},
        }))
        pc.run("create", "-f", manifest(tmp_path, "scope", "agent", {
            "id": scope_id,
            "description": "Names the in-scope services for the nightly sweep.",
            "model": {"provider_id": pid, "model_name": scenario},
            "tools": [],
            "system_prompt": [
                "You output ONLY a JSON object of the services to audit, as "
                "{\"services\": [{\"name\": \"...\", \"expr\": \"...\"}]}. Each "
                "expr is the service's posture-score check."
            ],
        }))

        # --- The compliance-sweep graph (create -f manifest) -------------
        response_format = {
            "type": "object", "required": ["services"],
            "properties": {"services": {"type": "array", "items": {
                "type": "object", "required": ["name", "expr"],
                "properties": {"name": {"type": "string"},
                               "expr": {"type": "string"}}}}},
        }
        nodes = [
            {"kind": "begin", "id": "begin"},
            {"kind": "agent", "id": "list_scope", "agent_id": scope_id,
             "input_template": "List the services to audit.",
             "response_format": response_format},
            {"kind": "fan_out", "id": "fan", "specs": [{
                "kind": "map", "target_node_id": "audit",
                "source_node_id": "list_scope", "source_path": "services",
                "on_failure": "collect"}]},
            {"kind": "tool_call", "id": "audit", "tool_id": "misc__calculate",
             "arguments_template": '{"expression": "{{ fanout_item.expr }}"}',
             "output_schema": {
                 "type": "object", "required": ["expression", "result"],
                 "properties": {"expression": {"type": "string"},
                                "result": {"type": "number"}}}},
            {"kind": "fan_in", "id": "report", "aggregate_template": (
                "COMPLIANCE POSTURE REPORT\n"
                "{% for r in nodes.audit %}service #{{ loop.index0 }}: "
                "{% if r.error %}FAILED ({{ r.ended_detail }})"
                "{% else %}OK score={{ r.parsed.result }}{% endif %}\n"
                "{% endfor %}")},
            {"kind": "end", "id": "end", "output_template": "{{ nodes.report.text }}"},
        ]
        edges = [
            {"kind": "static", "from_node": "begin", "to_node": "list_scope"},
            {"kind": "static", "from_node": "list_scope", "to_node": "fan"},
            {"kind": "static", "from_node": "audit", "to_node": "report"},
            {"kind": "static", "from_node": "report", "to_node": "end"},
        ]
        pc.run("create", "-f", manifest(tmp_path, "graph", "graph", {
            "id": gid,
            "description": "Nightly fan-out audit that survives a failing branch.",
            "max_iterations": 20, "nodes": nodes, "edges": edges,
        }))

        # --- Local workspace --------------------------------------------
        pc.run("create", "-f", manifest(tmp_path, "wp", "workspace_provider", {
            "id": wp, "provider": "local",
            "config": {"kind": "local", "root_path": str(tmp_path)},
        }))
        pc.run("create", "-f", manifest(tmp_path, "tpl", "workspace_template", {
            "id": tpl, "description": "cmpl cli", "provider_id": wp,
            "backend": {"kind": "local"},
        }))
        wid = pc.run(
            "create", "workspace", "--set", f"template_id={tpl}",
        ).stdout.split("/")[1].split()[0]

        # --- The scheduled trigger (create -f) + graph_fresh subscription -
        # A cron fixed far in the future (02:00 Jan 1) so the scheduler never
        # fires it on its own; the test drives it via `call trigger fire-now`.
        out = pc.run("create", "-f", manifest(tmp_path, "trig", "trigger", {
            "slug": slug, "name": f"Nightly compliance sweep {sfx}",
            "config": {"kind": "scheduled", "cron": "0 2 1 1 *",
                       "timezone": "Asia/Dubai", "catchup": "none"},
            "enabled": True,
        })).stdout
        # `create -f` echoes `<name>/<server-id> created`; parse the server id.
        trigger_id = out.split("/", 1)[1].split()[0]
        cleanup.insert(0, ("trigger", trigger_id))

        # The subscription is nested under the trigger (no top-level resource),
        # so it is created with the `call trigger subscriptions <id>` custom op.
        sub_file = tmp_path / "sub.yaml"
        sub_file.write_text(yaml.safe_dump({
            "config": {"kind": "graph_fresh_session",
                       "graph_id": gid, "workspace_id": wid},
            "payload_template": json.dumps({"run": "nightly"}),
            "parallelism": "skip",
        }))
        sub_out = pc.run(
            "call", "trigger", "subscriptions", trigger_id, "-f", str(sub_file),
            "-o", "json",
        ).stdout
        sub = json.loads(sub_out)
        sub_id = sub.get("id")
        assert sub_id, f"subscription create returned no id: {sub_out!r}"

        # --- Fire the trigger by hand (stand in for the 02:00 cron) ------
        empty = tmp_path / "empty.yaml"
        empty.write_text("{}\n")
        fire = json.loads(pc.run(
            "call", "trigger", "fire-now", trigger_id, "-f", str(empty),
            "-o", "json",
        ).stdout)
        assert not fire.get("skipped"), fire
        sid = None
        for res in fire.get("results", []):
            if res.get("ok") and res.get("artefact_id"):
                sid = res["artefact_id"]
                break
        assert sid, f"fire-now did not dispatch a graph session: {fire}"

        # --- Poll the fired graph session to terminal (via primectl) -----
        final = _poll_session(pc, sid, timeout_s=120)

        # (1) the graph_fresh_session-fired graph ran to terminal completed --
        # even though one branch failed (the collect proof at the session
        # level) -- and is tagged with the subscription id.
        assert final.get("ended_reason") == "completed", (
            f"the fired graph did not complete (collect should keep it alive): "
            f"{final}"
        )
        assert (final.get("metadata") or {}).get("subscription_id") == sub_id, (
            f"dispatched session not tagged with the subscription id: {final}"
        )

        # (2) map dispatched one isolated instance per service.
        state = json.loads(_files_get(pc, wid, f".state/graphs/{sid}/state.json"))
        assert state["ended_reason"] == "completed", state
        node_states = state["node_states"]
        audit_states = {
            k: v for k, v in node_states.items() if k.startswith("audit[")
        }
        assert set(audit_states) == {f"audit[{i}]" for i in range(_N)}, (
            f"map instance ids not contiguous 0..{_N - 1}: {sorted(audit_states)}"
        )

        # (3) exactly the unreachable branch is collected as failed; the rest
        # survive; the graph still completed.
        failed = {k for k, v in audit_states.items() if v.get("status") == "failed"}
        ended = {k for k, v in audit_states.items() if v.get("status") == "ended"}
        assert failed == {f"audit[{_FAIL_INDEX}]"}, (
            f"exactly the unreachable branch should be collected as failed; "
            f"failed={sorted(failed)}"
        )
        assert len(ended) == _N - 1, f"the reachable branches should survive; ended={sorted(ended)}"

        # (4) fan_in aggregated ALL branches: survivors render their score, the
        # collected branch renders a FAILED marker.
        report = _files_get(pc, wid, f".state/sessions/{sid}/messages.jsonl")
        assert "COMPLIANCE POSTURE REPORT" in report, report
        assert f"service #{_FAIL_INDEX}: FAILED (tool_output_invalid)" in report, (
            f"the collected branch was not rendered FAILED in the report: {report!r}"
        )
        assert report.count("OK score=") == _N - 1, (
            f"expected {_N - 1} surviving OK lines in the report: {report!r}"
        )
    finally:
        for res, ident in cleanup:
            pc.run("delete", res, ident, check=False)
