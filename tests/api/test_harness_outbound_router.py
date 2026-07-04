"""Outbound harness API surface — Spec B §8."""

from __future__ import annotations

import io
import tarfile
from datetime import datetime, timezone

import pytest

from primer.model.agent import Agent
from primer.model.harness import (
    Harness,
    HarnessDirection,
    HarnessOperation,
    HarnessStatus,
    TrackedEntity,
)


def _make_agent(*, id: str = "ag-bot", harness_id: str | None = None) -> Agent:
    return Agent(
        id=id,
        name="Bot",
        description="d",
        harness_id=harness_id,
        model={"provider_id": "openai", "model_name": "gpt-4"},
        temperature=0.2,
        tools=[],
        created_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_outbound_harness(**kwargs) -> Harness:
    defaults: dict = dict(
        id="hns_outbound01",
        slug="outbound-h",
        name="Outbound Harness",
        git_url="https://github.com/example/outbound",
        direction=HarnessDirection.OUTBOUND,
        status=HarnessStatus.DRAFT,
        created_at=datetime.now(timezone.utc),
        tracked_entities=[
            TrackedEntity(
                kind="agent",
                source_id="ag-bot",
                template_name="assistant",
            ),
        ],
    )
    defaults.update(kwargs)
    return Harness(**defaults)


def _make_inbound_harness(**kwargs) -> Harness:
    defaults: dict = dict(
        id="hns_inbound001",
        slug="inbound-h",
        name="Inbound Harness",
        git_url="https://github.com/example/inbound",
        direction=HarnessDirection.INBOUND,
        status=HarnessStatus.DRAFT,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kwargs)
    return Harness(**defaults)


# ---------------------------------------------------------------------------
# 1. POST /v1/harnesses with direction=outbound + tracked_entities → 201
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_outbound_harness_with_tracked_entities(client):
    r = await client.post(
        "/v1/harnesses",
        json={
            "name": "Outbound One",
            "slug": "outbound-one",
            "git_url": "https://github.com/x/y",
            "direction": "outbound",
            "tracked_entities": [
                {
                    "kind": "agent",
                    "source_id": "ag-1",
                    "template_name": "assistant",
                    "overrides": [
                        {
                            "field_path": "/model/provider_id",
                            "override_path": "llm.provider_id",
                            "widget": "llm-provider-picker",
                        },
                    ],
                },
            ],
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["direction"] == "outbound"
    assert len(body["tracked_entities"]) == 1
    assert body["tracked_entities"][0]["template_name"] == "assistant"


# ---------------------------------------------------------------------------
# 2. POST inbound with tracked_entities → 422
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_inbound_with_tracked_entities_rejected(client):
    r = await client.post(
        "/v1/harnesses",
        json={
            "name": "Bad Inbound",
            "slug": "bad-inbound",
            "git_url": "https://github.com/x/y",
            "direction": "inbound",
            "tracked_entities": [
                {
                    "kind": "agent",
                    "source_id": "ag-1",
                    "template_name": "assistant",
                },
            ],
        },
    )
    assert r.status_code == 422
    assert r.json()["code"] == "tracked_entities_on_inbound"


# ---------------------------------------------------------------------------
# 3. POST outbound with duplicate template_names → 422
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_outbound_duplicate_template_names_rejected(client):
    r = await client.post(
        "/v1/harnesses",
        json={
            "name": "Dup",
            "slug": "dup-tn",
            "git_url": "https://github.com/x/y",
            "direction": "outbound",
            "tracked_entities": [
                {"kind": "agent", "source_id": "ag-1", "template_name": "assistant"},
                {"kind": "agent", "source_id": "ag-2", "template_name": "assistant"},
            ],
        },
    )
    assert r.status_code == 422
    assert r.json()["code"] == "outbound_template_name_collision"


# ---------------------------------------------------------------------------
# 4. PUT /tracked_entities on outbound → 200, clears bundle_hash, status=DRAFT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_tracked_entities_outbound(client, app, fake_storage_provider):
    harness = _make_outbound_harness(
        id="hns_pte_ok",
        slug="pte-ok",
        status=HarnessStatus.INSTALLED,
        bundle_hash="abc123",
    )
    storage = fake_storage_provider.get_storage(Harness)
    await storage.create(harness)

    r = await client.put(
        "/v1/harnesses/hns_pte_ok/tracked_entities",
        json={
            "tracked_entities": [
                {
                    "kind": "agent",
                    "source_id": "ag-new",
                    "template_name": "helper",
                },
            ],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["tracked_entities"]) == 1
    assert body["tracked_entities"][0]["template_name"] == "helper"
    assert body["bundle_hash"] is None
    assert body["status"] == "draft"


# ---------------------------------------------------------------------------
# 5. PUT /tracked_entities on inbound → 409
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_tracked_entities_inbound_rejected(client, app, fake_storage_provider):
    harness = _make_inbound_harness(id="hns_pte_in", slug="pte-in")
    storage = fake_storage_provider.get_storage(Harness)
    await storage.create(harness)

    r = await client.put(
        "/v1/harnesses/hns_pte_in/tracked_entities",
        json={"tracked_entities": []},
    )
    assert r.status_code == 409
    assert r.json()["code"] == "direction_mismatch"


# ---------------------------------------------------------------------------
# PUT /tracked_entities — 422 on duplicate template names
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_tracked_entities_duplicate_names_rejected(client, app, fake_storage_provider):
    harness = _make_outbound_harness(id="hns_pte_dup", slug="pte-dup")
    storage = fake_storage_provider.get_storage(Harness)
    await storage.create(harness)

    r = await client.put(
        "/v1/harnesses/hns_pte_dup/tracked_entities",
        json={
            "tracked_entities": [
                {"kind": "agent", "source_id": "ag-1", "template_name": "x"},
                {"kind": "agent", "source_id": "ag-2", "template_name": "x"},
            ],
        },
    )
    assert r.status_code == 422
    assert r.json()["code"] == "outbound_template_name_collision"


# ---------------------------------------------------------------------------
# PUT /tracked_entities — 404 on missing harness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_tracked_entities_not_found(client):
    r = await client.put(
        "/v1/harnesses/hns_nope/tracked_entities",
        json={"tracked_entities": []},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 6. POST /build on outbound → 202, pending_operation=BUILD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_outbound_enqueues(client, app, fake_storage_provider):
    harness = _make_outbound_harness(id="hns_bld_ok", slug="bld-ok")
    storage = fake_storage_provider.get_storage(Harness)
    await storage.create(harness)

    r = await client.post("/v1/harnesses/hns_bld_ok/build")
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["pending_operation"] == "build"


# ---------------------------------------------------------------------------
# 7. POST /build on inbound → 409
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_inbound_rejected(client, app, fake_storage_provider):
    harness = _make_inbound_harness(id="hns_bld_in", slug="bld-in")
    storage = fake_storage_provider.get_storage(Harness)
    await storage.create(harness)

    r = await client.post("/v1/harnesses/hns_bld_in/build")
    assert r.status_code == 409
    assert r.json()["code"] == "direction_mismatch"


# ---------------------------------------------------------------------------
# 8. POST /build on outbound with no tracked_entities → 422
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_outbound_no_entities_rejected(client, app, fake_storage_provider):
    harness = _make_outbound_harness(
        id="hns_bld_empty",
        slug="bld-empty",
        tracked_entities=[],
    )
    storage = fake_storage_provider.get_storage(Harness)
    await storage.create(harness)

    r = await client.post("/v1/harnesses/hns_bld_empty/build")
    assert r.status_code == 422
    assert r.json()["code"] == "outbound_no_entities"


# ---------------------------------------------------------------------------
# 9. POST /build when pending_operation set → 409 (operation_in_flight)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_with_pending_op_rejected(client, app, fake_storage_provider):
    harness = _make_outbound_harness(
        id="hns_bld_pending",
        slug="bld-pending",
        pending_operation=HarnessOperation.BUILD,
    )
    storage = fake_storage_provider.get_storage(Harness)
    await storage.create(harness)

    r = await client.post("/v1/harnesses/hns_bld_pending/build")
    assert r.status_code == 409
    assert r.json()["code"] == "operation_in_flight"


# ---------------------------------------------------------------------------
# 10. POST /push on outbound → 202
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_outbound_enqueues(client, app, fake_storage_provider):
    harness = _make_outbound_harness(id="hns_push_ok", slug="push-ok")
    storage = fake_storage_provider.get_storage(Harness)
    await storage.create(harness)

    r = await client.post("/v1/harnesses/hns_push_ok/push")
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["pending_operation"] == "push"


# ---------------------------------------------------------------------------
# 11. POST /push on inbound → 409
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_inbound_rejected(client, app, fake_storage_provider):
    harness = _make_inbound_harness(id="hns_push_in", slug="push-in")
    storage = fake_storage_provider.get_storage(Harness)
    await storage.create(harness)

    r = await client.post("/v1/harnesses/hns_push_in/push")
    assert r.status_code == 409
    assert r.json()["code"] == "direction_mismatch"


# ---------------------------------------------------------------------------
# POST /push with no tracked_entities → 422
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_outbound_no_entities_rejected(client, app, fake_storage_provider):
    harness = _make_outbound_harness(
        id="hns_push_empty",
        slug="push-empty",
        tracked_entities=[],
    )
    storage = fake_storage_provider.get_storage(Harness)
    await storage.create(harness)

    r = await client.post("/v1/harnesses/hns_push_empty/push")
    assert r.status_code == 422
    assert r.json()["code"] == "outbound_no_entities"


# ---------------------------------------------------------------------------
# 12. POST /fetch on outbound → 409
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_on_outbound_rejected(client, app, fake_storage_provider):
    harness = _make_outbound_harness(id="hns_fetch_out", slug="fetch-out")
    storage = fake_storage_provider.get_storage(Harness)
    await storage.create(harness)

    r = await client.post("/v1/harnesses/hns_fetch_out/fetch")
    assert r.status_code == 409
    assert r.json()["code"] == "direction_mismatch"


# ---------------------------------------------------------------------------
# 13. POST /install on outbound → 409
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_on_outbound_rejected(client, app, fake_storage_provider):
    harness = _make_outbound_harness(
        id="hns_inst_out",
        slug="inst-out",
        status=HarnessStatus.READY,
    )
    storage = fake_storage_provider.get_storage(Harness)
    await storage.create(harness)

    r = await client.post("/v1/harnesses/hns_inst_out/install")
    assert r.status_code == 409
    assert r.json()["code"] == "direction_mismatch"


# ---------------------------------------------------------------------------
# 14. POST /sync on outbound → 409
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_on_outbound_rejected(client, app, fake_storage_provider):
    harness = _make_outbound_harness(
        id="hns_sync_out",
        slug="sync-out",
        status=HarnessStatus.INSTALLED,
        available_bundle_hash="abc",
    )
    storage = fake_storage_provider.get_storage(Harness)
    await storage.create(harness)

    r = await client.post("/v1/harnesses/hns_sync_out/sync")
    assert r.status_code == 409
    assert r.json()["code"] == "direction_mismatch"


# ---------------------------------------------------------------------------
# 15. GET /v1/harnesses?direction=outbound filters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_filter_by_direction(client, app, fake_storage_provider):
    storage = fake_storage_provider.get_storage(Harness)
    await storage.create(_make_inbound_harness(id="hns_filter_in", slug="filter-in"))
    await storage.create(_make_outbound_harness(id="hns_filter_out", slug="filter-out"))

    # outbound filter → just the outbound one
    r_out = await client.get("/v1/harnesses?direction=outbound")
    assert r_out.status_code == 200
    out_ids = [item["id"] for item in r_out.json()["items"]]
    assert "hns_filter_out" in out_ids
    assert "hns_filter_in" not in out_ids

    # inbound filter → just the inbound one
    r_in = await client.get("/v1/harnesses?direction=inbound")
    assert r_in.status_code == 200
    in_ids = [item["id"] for item in r_in.json()["items"]]
    assert "hns_filter_in" in in_ids
    assert "hns_filter_out" not in in_ids

    # No filter → both present
    r_all = await client.get("/v1/harnesses")
    assert r_all.status_code == 200
    all_ids = [item["id"] for item in r_all.json()["items"]]
    assert "hns_filter_in" in all_ids
    assert "hns_filter_out" in all_ids


# ---------------------------------------------------------------------------
# Default direction defaults to inbound when omitted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_defaults_to_inbound(client):
    r = await client.post(
        "/v1/harnesses",
        json={
            "name": "Default",
            "slug": "default-direction",
            "git_url": "https://github.com/x/y",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["direction"] == "inbound"
    assert body["tracked_entities"] == []


# ---------------------------------------------------------------------------
# GET /v1/harnesses/{id}/bundle.tar.gz — download the built bundle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_bundle_outbound(client, app, fake_storage_provider):
    # The tracked agent must exist — download builds the bundle from the DB.
    await fake_storage_provider.get_storage(Agent).create(_make_agent(id="ag-bot"))
    harness = _make_outbound_harness(id="hns_dl_ok", slug="dl-ok")
    await fake_storage_provider.get_storage(Harness).create(harness)

    r = await client.get("/v1/harnesses/hns_dl_ok/bundle.tar.gz")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/gzip"
    disp = r.headers["content-disposition"]
    assert "attachment" in disp
    assert "dl-ok-" in disp and disp.endswith('.tar.gz"')

    tf = tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz")
    names = set(tf.getnames())
    assert "harness.yaml" in names
    assert "overrides.schema.json" in names
    assert "templates/assistant.yaml" in names
    # the agent template is non-empty and mentions the entity kind
    member = tf.extractfile("templates/assistant.yaml").read().decode()
    assert "kind: agent" in member


@pytest.mark.asyncio
async def test_download_bundle_no_entities(client, app, fake_storage_provider):
    harness = _make_outbound_harness(
        id="hns_dl_empty", slug="dl-empty", tracked_entities=[],
    )
    await fake_storage_provider.get_storage(Harness).create(harness)

    r = await client.get("/v1/harnesses/hns_dl_empty/bundle.tar.gz")
    assert r.status_code == 422
    assert r.json()["code"] == "outbound_no_entities"


@pytest.mark.asyncio
async def test_download_bundle_inbound_rejected(client, app, fake_storage_provider):
    harness = _make_inbound_harness(id="hns_dl_in", slug="dl-in")
    await fake_storage_provider.get_storage(Harness).create(harness)

    r = await client.get("/v1/harnesses/hns_dl_in/bundle.tar.gz")
    assert r.status_code == 409
    assert r.json()["code"] == "direction_mismatch"


@pytest.mark.asyncio
async def test_download_bundle_not_found(client):
    r = await client.get("/v1/harnesses/hns_nope/bundle.tar.gz")
    assert r.status_code == 404
