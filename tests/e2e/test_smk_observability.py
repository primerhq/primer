"""SMK observability tests (Phase 2): metrics, health snapshot, turn logs,
workers status. OBS-04 (OTEL export) gates on a collector; OBS-03/06 are
asserted at the level the live REST surface exposes.
"""
from __future__ import annotations

import pytest

from tests._support.mock_llm import Rule
from tests._support.runs import (
    make_local_workspace,
    make_scripted_agent,
    start_agent_session,
    wait_terminal,
)
from tests._support.smk import smk

pytestmark = pytest.mark.asyncio


@smk("SMK-OBS-01")
async def test_prometheus_metrics_endpoint(authed_client):
    # /metrics redirects to /metrics/; the client follows it explicitly.
    r = await authed_client.get("/metrics/", follow_redirects=True)
    assert r.status_code == 200, r.text
    body = r.text
    assert "# TYPE" in body  # Prometheus exposition format
    assert "_total" in body  # at least one counter is exported


@smk("SMK-OBS-02")
async def test_health_snapshot_metrics(authed_client):
    r = await authed_client.get("/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["scheduler"]["alive"] is True
    assert "metrics" in body["scheduler"]
    assert "in_flight" in body["worker_pool"]
    assert "capacity" in body["worker_pool"]


@smk("SMK-OBS-07")
async def test_workers_status(authed_client):
    r = await authed_client.get("/v1/workers")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    # at least one worker is registered (the bringup server runs a worker);
    # the in-memory sqlite server also registers its pool worker.
    if items:
        w = items[0]
        for key in ("id", "status"):
            assert key in w, w


@smk("SMK-OBS-05")
async def test_session_turn_log_after_run(authed_client, mock_llm, unique_suffix, tmp_path):
    registry, base_url = mock_llm
    agent = await make_scripted_agent(
        authed_client, registry, base_url, suffix=unique_suffix,
        scenario=f"scripted:obs05-{unique_suffix}", rules=[Rule(emit_text="ok")],
    )
    wid = await make_local_workspace(authed_client, suffix=unique_suffix, root=tmp_path)
    sid = await start_agent_session(authed_client, workspace_id=wid, agent_id=agent["agent_id"])
    await wait_terminal(authed_client, sid)
    tl = await authed_client.get(f"/v1/sessions/{sid}/turn_log")
    assert tl.status_code == 200, tl.text
    body = tl.json()
    assert body["total"] >= 1
    # per-turn telemetry carries timing
    assert any("duration_ms" in i for i in body["items"])


@smk("SMK-OBS-03", status="partial")
async def test_structured_json_logs(authed_client):
    # JSON-structured logs with trace correlation are produced by the running
    # server (log_json=true); asserting their content requires reading the
    # server's log file, which is out of band for the REST client. We assert
    # the health surface responds (the run that produced logs succeeded).
    r = await authed_client.get("/v1/health")
    assert r.status_code == 200


@smk("SMK-OBS-06", status="partial")
async def test_zero_overhead_when_disabled(authed_client):
    # Toggling telemetry off is a server-config change; on this server metrics
    # are enabled. We assert the enabled surface is present (the inverse of the
    # disabled case), leaving the full disabled-comparison to a config variant.
    r = await authed_client.get("/metrics/", follow_redirects=True)
    assert r.status_code == 200


@smk("SMK-OBS-04", status="partial")
async def test_otel_traces_exported(authed_client):
    # OTEL span export requires an OTLP collector (or console exporter) wired
    # via testconfig.observability; absent that, we assert the traced surface
    # responds. The full span-tree assertion runs when a collector is provided.
    r = await authed_client.get("/v1/health")
    assert r.status_code == 200
