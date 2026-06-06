"""SMK event-driven tests (Phase 2): trigger CRUD + delayed/scheduled config.

ask_user park/respond (EVT-01/02), schema rejection (EVT-03) and yield cancel
(EVT-05) are covered by the existing injection-based journeys, which are tagged
with their SMK ids in place (driving the park via a live agent is
non-deterministic). Trigger *firing* + subscription kinds + catchup +
parallelism (EVT-08/09/10/12) and the ask_user timeout (EVT-04) depend on the
scheduler/worker timing and remain to be wired against the distributed lane.
"""
from __future__ import annotations

import pytest

from tests._support.smk import smk

pytestmark = pytest.mark.asyncio


@smk("SMK-EVT-11")
async def test_trigger_crud_and_enable_disable(authed_client, unique_suffix):
    slug = f"smk-{unique_suffix}"[:64]
    body = {
        "slug": slug, "name": "smk trigger",
        "config": {"kind": "delayed", "fire_at": "2099-01-01T00:00:00Z"},
    }
    create = await authed_client.post("/v1/triggers", json=body)
    assert create.status_code in (200, 201), create.text
    tid = create.json()["id"]
    got = await authed_client.get(f"/v1/triggers/{tid}")
    assert got.status_code == 200
    upd = await authed_client.put(f"/v1/triggers/{tid}", json={**body, "enabled": False})
    assert upd.status_code in (200, 204), upd.text
    delete = await authed_client.delete(f"/v1/triggers/{tid}")
    assert delete.status_code in (200, 204), delete.text


@smk("SMK-EVT-06")
async def test_delayed_trigger_schedules_fire(authed_client, unique_suffix):
    slug = f"smkd-{unique_suffix}"[:64]
    r = await authed_client.post(
        "/v1/triggers",
        json={"slug": slug, "name": "d", "config": {"kind": "delayed", "fire_at": "2099-01-01T00:00:00Z"}},
    )
    assert r.status_code in (200, 201), r.text
    assert r.json().get("next_fire_at")


@smk("SMK-EVT-07")
async def test_scheduled_trigger_cron_validation(authed_client, unique_suffix):
    slug = f"smks-{unique_suffix}"[:64]
    ok = await authed_client.post(
        "/v1/triggers",
        json={"slug": slug, "name": "s",
              "config": {"kind": "scheduled", "cron": "0 9 * * 1-5", "timezone": "UTC"}},
    )
    assert ok.status_code in (200, 201), ok.text
    bad = await authed_client.post(
        "/v1/triggers",
        json={"slug": f"{slug}b"[:64], "name": "s",
              "config": {"kind": "scheduled", "cron": "not a cron", "timezone": "UTC"}},
    )
    assert bad.status_code == 422, bad.text
