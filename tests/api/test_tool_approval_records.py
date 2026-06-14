"""GET /v1/tool_approval/records: resolved approval-decision history."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from primer.model.tool_approval import ToolApprovalRecord


def _rec(*, id_: str, decision: str, decided_at: datetime) -> ToolApprovalRecord:
    return ToolApprovalRecord(
        id=id_,
        tool_name="delete_workspace",
        toolset_id="workspaces",
        arguments={"id": "ws-x"},
        tool_call_id=f"call-{id_}",
        session_id="sess-1",
        decided_at=decided_at,
        decision=decision,  # type: ignore[arg-type]
        policy_id="p1",
        approval_type="required",
    )


async def _seed(app) -> None:
    storage = app.state.storage_provider.get_storage(ToolApprovalRecord)
    base = datetime(2026, 6, 14, 9, 0, tzinfo=UTC)
    await storage.create(_rec(id_="r1", decision="approved", decided_at=base))
    await storage.create(
        _rec(id_="r2", decision="rejected", decided_at=base + timedelta(minutes=5))
    )
    await storage.create(
        _rec(id_="r3", decision="approved", decided_at=base + timedelta(minutes=10))
    )
    await storage.create(
        _rec(id_="r4", decision="timeout", decided_at=base + timedelta(minutes=2))
    )


@pytest.mark.asyncio
async def test_records_list_all_ordered_time_desc(client, app):
    await _seed(app)
    r = await client.get("/v1/tool_approval/records")
    assert r.status_code == 200, r.text
    body = r.json()
    ids = [it["id"] for it in body["items"]]
    assert ids == ["r3", "r2", "r4", "r1"]  # newest decided_at first
    assert body["total"] == 4


@pytest.mark.asyncio
async def test_records_list_filter_by_status(client, app):
    await _seed(app)
    r = await client.get("/v1/tool_approval/records?status=approved")
    assert r.status_code == 200, r.text
    body = r.json()
    ids = [it["id"] for it in body["items"]]
    assert ids == ["r3", "r1"]
    assert all(it["decision"] == "approved" for it in body["items"])


@pytest.mark.asyncio
async def test_records_list_filter_rejected(client, app):
    await _seed(app)
    r = await client.get("/v1/tool_approval/records?status=rejected")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [it["id"] for it in body["items"]] == ["r2"]


@pytest.mark.asyncio
async def test_records_list_pagination(client, app):
    await _seed(app)
    r = await client.get("/v1/tool_approval/records?offset=0&length=2")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["items"]) == 2
    assert [it["id"] for it in body["items"]] == ["r3", "r2"]


@pytest.mark.asyncio
async def test_records_list_empty(client):
    r = await client.get("/v1/tool_approval/records")
    assert r.status_code == 200, r.text
    assert r.json()["items"] == []


@pytest.mark.asyncio
async def test_records_list_bad_status_422(client):
    r = await client.get("/v1/tool_approval/records?status=bogus")
    assert r.status_code == 422
