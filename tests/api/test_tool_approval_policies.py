"""CRUD + validation tests for /v1/tool_approval_policies."""

from __future__ import annotations

import pytest


_REGO_OK = (
    "package matrix.tool_approval\n"
    "default required := false\n"
    "required if input.tool_name == \"x\"\n"
)
_REGO_BROKEN = "this is not valid rego"


@pytest.mark.asyncio
async def test_create_required_policy_ok(client):
    body = {
        "id": "p-req-1",
        "toolset_id": "system",
        "tool_name": "delete_session",
        "approval": {"type": "required"},
    }
    r = await client.post("/v1/tool_approval_policies", json=body)
    assert r.status_code == 201, r.text
    assert r.json()["enabled"] is True


@pytest.mark.asyncio
async def test_create_policy_rego_compile_fail_422(client):
    body = {
        "id": "p-rego-bad",
        "toolset_id": "system",
        "tool_name": "x",
        "approval": {"type": "policy", "policy": _REGO_BROKEN},
    }
    r = await client.post("/v1/tool_approval_policies", json=body)
    assert r.status_code == 422, r.text
    envelope = r.json()
    errors = envelope.get("extensions", {}).get("errors", [])
    assert any("policy" in (e.get("loc") or []) for e in errors)


@pytest.mark.asyncio
async def test_create_policy_rego_compile_ok(client):
    body = {
        "id": "p-rego-ok",
        "toolset_id": "system",
        "tool_name": "x",
        "approval": {"type": "policy", "policy": _REGO_OK},
    }
    r = await client.post("/v1/tool_approval_policies", json=body)
    assert r.status_code == 201, r.text


@pytest.mark.asyncio
async def test_create_llm_policy_unknown_provider_422(client):
    body = {
        "id": "p-llm-bad",
        "toolset_id": "system",
        "tool_name": "x",
        "approval": {
            "type": "llm",
            "provider_id": "does-not-exist",
            "model": "m",
            "prompt": "judge",
        },
    }
    r = await client.post("/v1/tool_approval_policies", json=body)
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_duplicate_toolset_tool_409(client):
    body = {
        "id": "p-1",
        "toolset_id": "system",
        "tool_name": "shell_exec",
        "approval": {"type": "required"},
    }
    r = await client.post("/v1/tool_approval_policies", json=body)
    assert r.status_code == 201
    dup = {**body, "id": "p-2"}
    r = await client.post("/v1/tool_approval_policies", json=dup)
    assert r.status_code == 409, r.text


@pytest.mark.asyncio
async def test_list_and_delete(client):
    body = {
        "id": "p-list",
        "toolset_id": "system",
        "tool_name": "y",
        "approval": {"type": "required"},
    }
    await client.post("/v1/tool_approval_policies", json=body)
    r = await client.get("/v1/tool_approval_policies")
    assert r.status_code == 200
    assert any(p["id"] == "p-list" for p in r.json()["items"])
    r = await client.delete("/v1/tool_approval_policies/p-list")
    assert r.status_code in (200, 204)
    r = await client.get("/v1/tool_approval_policies/p-list")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_invalidate_endpoint_returns_202(client):
    r = await client.post("/v1/tool_approval_policies/invalidate")
    assert r.status_code in (200, 202)
