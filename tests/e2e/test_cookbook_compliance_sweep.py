"""Cookbook recipe #11 regression: overnight compliance sweep.

A nightly ``scheduled`` trigger fires a GRAPH (not a single agent) through a
``graph_fresh_session`` subscription. The graph fans out one audit branch per
in-scope service via ``fan_out: map`` (one instance per list item), each branch
runs a deterministic audit, and ``on_failure: collect`` keeps the whole sweep
alive when one service is unreachable. A ``fan_in`` aggregates a posture report.

Recipe: primerhq.github.io/docs_source/cookbook/compliance-sweep.md

This is the FIRST cookbook to exercise ``graph_fresh_session`` end-to-end and
the ``map`` + ``on_failure: collect`` combination. It guards four mechanics:

  * the ``graph_fresh_session``-fired graph runs to TERMINAL ``completed`` with a
    real transcript (the graph leg of the slot-allocation fix -- not silently
    stranded);
  * ``fan_out: map`` dispatches one instance PER service with isolated
    per-instance state dirs (``nodes/audit[i]/`` -- never a shared dir);
  * a deliberately-failing branch (the ``payments-legacy`` service whose audit
    expression is ``1 / 0``) is COLLECTED -- the branch is marked failed, the
    graph still completes, and the report ships with the survivors + a FAILED
    marker;
  * ``fan_in`` aggregates every branch output.

The audit step is a ``tool_call`` map target (``misc__calculate``) so the whole
audit leg is deterministic on any model: a valid expression yields
``{"expression", "result"}`` (conforms to the node's ``output_schema``); the
unreachable service's ``1 / 0`` yields an error string that does NOT conform ->
``tool_output_invalid`` -> the branch fails -> ``collect`` keeps the sweep going.
Only the scope-lister uses the LLM, and it is scripted with the deterministic
mock so the map source list is fixed.

Run with:
    PRIMER_RUN_E2E=1 uv run pytest tests/e2e/test_cookbook_compliance_sweep.py -n0 -q
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
    wait_terminal,
)
from tests._support.smk import smk

pytestmark = [pytest.mark.asyncio]


# The watchlist. Each service carries the audit `expr` its check computes.
# `payments-legacy` is the unreachable one: `1 / 0` errors, the tool's output
# is the error string (not the {"expression","result"} object), the node's
# output_schema rejects it -> tool_output_invalid -> the branch is collected.
_SERVICES = [
    {"name": "billing-api", "expr": "90 + 5"},
    {"name": "auth-svc", "expr": "80 + 8"},
    {"name": "payments-legacy", "expr": "1 / 0"},  # unreachable -> fails
    {"name": "search-svc", "expr": "70 + 9"},
]
_N = len(_SERVICES)
_FAIL_INDEX = 2  # payments-legacy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_scheduled_trigger(client, *, slug: str) -> dict:
    """A scheduled trigger fixed far in the future so the scheduler never
    fires it on its own; the test drives it via fire_now."""
    r = await client.post(
        "/v1/triggers",
        json={
            "slug": slug,
            "name": f"E2E compliance sweep {slug}",
            "config": {
                "kind": "scheduled",
                "cron": "0 2 1 1 *",  # 02:00 on Jan 1 -- effectively never
                "timezone": "Asia/Dubai",
                "catchup": "none",
            },
            "enabled": True,
        },
    )
    assert r.status_code in (200, 201), r.text
    return r.json()


async def _create_graph_fresh_sub(
    client, *, trigger_id: str, graph_id: str, workspace_id: str, payload: str,
) -> dict:
    r = await client.post(
        f"/v1/triggers/{trigger_id}/subscriptions",
        json={
            "config": {
                "kind": "graph_fresh_session",
                "graph_id": graph_id,
                "workspace_id": workspace_id,
            },
            "payload_template": payload,
            "parallelism": "skip",
        },
    )
    assert r.status_code in (200, 201), r.text
    return r.json()


def _dispatched_session_id(fire_result: dict) -> str | None:
    for res in fire_result.get("results", []):
        if res.get("ok") and res.get("artefact_id"):
            return res["artefact_id"]
    return None


def _graph_state(tmp_path: Path, wid: str, sid: str) -> dict:
    p = tmp_path / wid / ".state" / "graphs" / sid / "state.json"
    assert p.exists(), f"graph state.json missing at {p} (the fired graph was stranded)"
    return json.loads(p.read_text(encoding="utf-8"))


def _session_report(tmp_path: Path, wid: str, sid: str) -> str:
    p = tmp_path / wid / ".state" / "sessions" / sid / "messages.jsonl"
    assert p.exists(), f"session transcript missing at {p}"
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@smk("SMK-COOKBOOK-11")
async def test_nightly_sweep_collects_failing_branch(
    authed_client, mock_llm, unique_suffix, tmp_path,
):
    registry, base_url = mock_llm
    sfx = f"cmpl-{unique_suffix}"
    cleanup: list[str] = []
    try:
        # scope-lister: scripted to emit the structured services list verbatim.
        scope = await make_scripted_agent(
            authed_client, registry, base_url, suffix=sfx,
            scenario=f"scripted:{sfx}",
            system_prompt=["Emit the in-scope services as JSON."],
            rules=[Rule(emit_text=json.dumps({"services": _SERVICES}))],
        )

        # The compliance-sweep graph (spec UC2 shape, deterministic audit leg).
        response_format = {
            "type": "object",
            "required": ["services"],
            "properties": {
                "services": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "expr"],
                        "properties": {
                            "name": {"type": "string"},
                            "expr": {"type": "string"},
                        },
                    },
                }
            },
        }
        nodes = [
            {"kind": "begin", "id": "begin"},
            {
                "kind": "agent", "id": "list_scope",
                "agent_id": scope["agent_id"],
                "input_template": "List the services to audit.",
                "response_format": response_format,
            },
            {
                "kind": "fan_out", "id": "fan",
                "specs": [{
                    "kind": "map",
                    "target_node_id": "audit",
                    "source_node_id": "list_scope",
                    "source_path": "services",
                    "on_failure": "collect",
                }],
            },
            {
                # One audit per service. A tool_call map target reads the
                # per-instance fanout_item (the fix that lets a fan-out
                # tool_call target see fanout_item/fanout_index).
                "kind": "tool_call", "id": "audit",
                "tool_id": "misc__calculate",
                "arguments_template": '{"expression": "{{ fanout_item.expr }}"}',
                "output_schema": {
                    "type": "object",
                    "required": ["expression", "result"],
                    "properties": {
                        "expression": {"type": "string"},
                        "result": {"type": "number"},
                    },
                },
            },
            {
                # collect: surviving branches render their score; the failed
                # branch carries .error / .ended_detail and is marked FAILED.
                "kind": "fan_in", "id": "report",
                "aggregate_template": (
                    "COMPLIANCE POSTURE REPORT\n"
                    "{% for r in nodes.audit %}"
                    "service #{{ loop.index0 }}: "
                    "{% if r.error %}FAILED ({{ r.ended_detail }})"
                    "{% else %}OK score={{ r.parsed.result }}{% endif %}\n"
                    "{% endfor %}"
                ),
            },
            {"kind": "end", "id": "end", "output_template": "{{ nodes.report.text }}"},
        ]
        edges = [
            {"kind": "static", "from_node": "begin", "to_node": "list_scope"},
            {"kind": "static", "from_node": "list_scope", "to_node": "fan"},
            {"kind": "static", "from_node": "audit", "to_node": "report"},
            {"kind": "static", "from_node": "report", "to_node": "end"},
        ]
        gid = await make_graph(
            authed_client, suffix=sfx, nodes=nodes, edges=edges,
            max_iterations=20,
        )

        wid = await make_local_workspace(authed_client, suffix=sfx, root=tmp_path)

        trigger = await _create_scheduled_trigger(
            authed_client, slug=f"e2e-cmpl-{unique_suffix}",
        )
        cleanup.append(f"/v1/triggers/{trigger['id']}")
        sub = await _create_graph_fresh_sub(
            authed_client, trigger_id=trigger["id"], graph_id=gid,
            workspace_id=wid, payload=json.dumps({"run": "nightly"}),
        )

        # Fire the trigger (stand in for the 02:00 cron).
        r = await authed_client.post(
            f"/v1/triggers/{trigger['id']}/fire_now", json={},
        )
        assert r.status_code == 200, r.text
        fire = r.json()
        assert not fire.get("skipped"), fire
        sid = _dispatched_session_id(fire)
        assert sid is not None, f"fire_now did not dispatch a graph session: {fire}"

        final = await wait_terminal(authed_client, sid, timeout_s=120)

        # (1) The graph_fresh_session-fired graph ran to TERMINAL completed --
        # even though one branch failed (the collect proof at the session
        # level) -- and wrote a real on-disk transcript (not stranded).
        assert final.get("status") == "ended", final
        assert final.get("ended_reason") == "completed", (
            f"the fired graph did not complete (collect should keep it alive "
            f"despite the failing branch): {final}"
        )
        assert (final.get("metadata") or {}).get("subscription_id") == sub["id"], (
            f"dispatched session not tagged with the subscription id: {final}"
        )

        state = _graph_state(tmp_path, wid, sid)
        assert state["ended_reason"] == "completed", state
        node_states = state["node_states"]

        # (2) map dispatched one instance PER service, each with an isolated
        # per-instance node id in the graph state.
        audit_states = {
            k: v for k, v in node_states.items() if k.startswith("audit[")
        }
        assert len(audit_states) == _N, (
            f"map should produce {_N} audit instances, got "
            f"{sorted(audit_states)}"
        )
        assert set(audit_states) == {f"audit[{i}]" for i in range(_N)}, (
            f"map instance ids not contiguous 0..{_N - 1}: {sorted(audit_states)}"
        )

        # (3) exactly ONE branch (the unreachable service) is collected as
        # failed; every other branch survived; the graph still completed.
        failed = {k for k, v in audit_states.items() if v.get("status") == "failed"}
        ended = {k for k, v in audit_states.items() if v.get("status") == "ended"}
        assert failed == {f"audit[{_FAIL_INDEX}]"}, (
            f"exactly the unreachable branch should be collected as failed; "
            f"failed={sorted(failed)}"
        )
        assert len(ended) == _N - 1, (
            f"the {_N - 1} reachable branches should survive; "
            f"ended={sorted(ended)}"
        )

        # (4) fan_in aggregated ALL branches: the surviving services render
        # their score, and the collected branch renders a FAILED marker.
        report = _session_report(tmp_path, wid, sid)
        assert "COMPLIANCE POSTURE REPORT" in report, report
        assert f"service #{_FAIL_INDEX}: FAILED (tool_output_invalid)" in report, (
            f"the collected branch was not rendered as FAILED in the fan_in "
            f"report: {report!r}"
        )
        assert report.count("OK score=") == _N - 1, (
            f"expected {_N - 1} surviving OK lines in the fan_in report: "
            f"{report!r}"
        )

        # The dispatched graph session ended in a pure data-shaping terminal:
        # the only assistant content is the rendered report (the End node's
        # output), not an LLM turn -- the audit leg was deterministic.
        assert '"kind": "assistant_token"' in report or "assistant_token" in report, (
            f"the graph's final assistant output was not committed to the "
            f"transcript: {report!r}"
        )
    finally:
        for url in reversed(cleanup):
            await authed_client.delete(url)
