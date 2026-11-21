"""Live MCP contracts for raw JSON bodies and exact-byte webhook signatures."""
from __future__ import annotations

import asyncio
import base64
from contextlib import AsyncExitStack
import json

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from primer.bootstrap.seed import default_policy_id
from tests._support.harness_git import build_harness_repo
from tests.e2e.test_services_publish_serve import FN, _tar
from tests.e2e.test_smk_harness import _wait_idle
from tests.e2e.test_webhook_trigger_e2e import _create_webhook_trigger, _hmac_sig

_TOOLS = ("harnesses__put_harness_overrides", "svc__svc_gateway_function", "webhooks__receive_webhook")


def _text(result):
    return "".join(getattr(part, "text", "") for part in result.content)


async def _call(session, tool, arguments, *, error_status=None):
    result = await session.call_tool("call_tool", arguments={"scoped_id": tool, "arguments": arguments})
    envelope = json.loads(_text(result))
    if error_status is not None:
        assert envelope["ok"] is False, envelope
        assert json.loads(envelope["error"]["message"])["status"] == error_status, envelope
        return None
    assert not result.is_error and envelope["ok"] is True, envelope
    return json.loads(envelope["output"])


@pytest.mark.asyncio
async def test_generated_raw_body_mcp_journey(client, base_url, unique_suffix, tmp_path):
    async def put(path, body):
        response = await client.put(path, json=body)
        assert response.status_code in (200, 204), response.text

    async def remove(path):
        response = await client.delete(path)
        assert response.status_code in (200, 202, 204, 404), response.text
        if response.status_code == 202:
            for _ in range(100):
                if (await client.get(path)).status_code == 404:
                    return
                await asyncio.sleep(0.3)
            pytest.fail(f"async cleanup did not remove {path}")

    async with AsyncExitStack() as cleanup:
        exposure = await client.get("/v1/mcp_exposure")
        assert exposure.status_code == 200, exposure.text
        cleanup.push_async_callback(put, "/v1/mcp_exposure", {
            key: exposure.json()[key] for key in ("enabled", "mode", "allowed_tools")
        })
        policies = {}
        for scoped in _TOOLS:
            toolset, tool = scoped.split("__", 1)
            path = "/v1/tool_approval_policies/" + default_policy_id(toolset, tool)
            policy = await client.get(path)
            assert policy.status_code == 200, policy.text
            policies[scoped] = (path, policy.json())
            cleanup.push_async_callback(put, path, policy.json())
        # First demonstrate the normal approval gate. Only then does this test's
        # operator opt these three tools out for the isolated roundtrip.
        await put(policies[_TOOLS[0]][0], {**policies[_TOOLS[0]][1], "enabled": True})
        await put("/v1/mcp_exposure", {"enabled": True, "mode": "allowlist", "allowed_tools": list(_TOOLS)})
        token = await client.post("/v1/auth/tokens", json={"name": f"raw-{unique_suffix}", "scopes": ["mcp"]})
        assert token.status_code == 201, token.text
        cleanup.push_async_callback(remove, f"/v1/auth/tokens/{token.json()['id']}")

        url = build_harness_repo(tmp_path / "raw-body-repo", name=f"raw-{unique_suffix}")
        created = await client.post("/v1/harnesses", json={
            "slug": f"raw-{unique_suffix}", "name": "MCP overrides", "git_url": url, "ref": "main",
        })
        assert created.status_code == 201, created.text
        hid = created.json()["id"]
        cleanup.push_async_callback(remove, f"/v1/harnesses/{hid}")
        fetched = await client.post(f"/v1/harnesses/{hid}/fetch")
        assert fetched.status_code == 202, fetched.text
        harness = await _wait_idle(client, hid)
        assert harness.get("overrides_schema") is not None, harness

        name = f"raw-function-{unique_suffix}"
        service = await client.post("/v1/services", json={"name": name, "description": "MCP function arguments"})
        assert service.status_code == 201, service.text
        sid = service.json()["id"]
        cleanup.push_async_callback(remove, f"/v1/services/{sid}")
        published = await client.post(f"/v1/services/{sid}/versions", content=_tar({"index.html": b"test", "functions.py": FN.encode()}), headers={"content-type": "application/gzip"})
        assert published.status_code == 201, published.text

        secret = "isolated-mcp-webhook-secret"
        trigger = await _create_webhook_trigger(client, slug=f"raw-{unique_suffix}", hmac_secret=secret)
        cleanup.push_async_callback(remove, f"/v1/triggers/{trigger['id']}")
        # No subscriptions: this journey never invokes a model or external target.
        body = b'{ "event": "exact bytes", "n": 2 }\r\n'
        webhook = {"token": trigger["config"]["token"], "body": base64.b64encode(body).decode(),
                   "signature": _hmac_sig(secret, body), "query": {"source": "mcp & test"}}
        overrides = {"profile_id": "unused-no-install", "description": "set through MCP"}
        async with create_mcp_http_client(headers={"Authorization": f"Bearer {token.json()['plaintext']}"}) as http_client:
            async with streamable_http_client(f"{base_url}/v1/mcp/", http_client=http_client) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    refused = await session.call_tool("call_tool", arguments={
                        "scoped_id": _TOOLS[0], "arguments": {"harness_id": hid, "body": overrides},
                    })
                    assert refused.is_error
                    assert json.loads(_text(refused))["error"]["code"] == "tool_unavailable"
                    assert (await client.get(f"/v1/harnesses/{hid}")).json()["overrides"] != overrides
                    for path, policy in policies.values():
                        await put(path, {**policy, "enabled": False})
                    for scoped in _TOOLS:
                        listed = await session.call_tool("list_tools", arguments={"text_prefix": scoped, "include_schema": True})
                        assert not listed.is_error, _text(listed)
                        item, = json.loads(_text(listed))["items"]
                        assert item["scoped_id"] == scoped
                        assert "body" in item["schema"]["properties"]
                    saved = await _call(session, _TOOLS[0], {"harness_id": hid, "body": overrides})
                    assert saved["overrides"] == overrides
                    await _call(session, _TOOLS[0], {"harness_id": hid, "body": {"profile_id": 12}}, error_status=422)
                    assert (await client.get(f"/v1/harnesses/{hid}")).json()["overrides"] == overrides
                    assert await _call(session, _TOOLS[1], {"name": name, "fn": "double", "body": {"n": 21}}) == 42
                    await _call(session, _TOOLS[1], {"name": name, "fn": "double", "body": {"n": "bad"}}, error_status=422)
                    accepted = await _call(session, _TOOLS[2], webhook)
                    assert accepted["status"] == "accepted"
                    await _call(session, _TOOLS[2], {**webhook, "signature": "sha256=bad"}, error_status=401)
                    delivery = await client.get(f"/v1/webhooks/{webhook['token']}/deliveries/{accepted['delivery_id']}")
                    assert delivery.status_code == 200, delivery.text
                    assert delivery.json()["delivery_id"] == accepted["delivery_id"]
