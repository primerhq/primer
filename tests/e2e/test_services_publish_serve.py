"""End-to-end: publish a service, serve it, call a bundle function, roll back."""

from __future__ import annotations

import base64
import io
import json
from contextlib import AsyncExitStack
import tarfile

import pytest

FN = (
    "@primer_tool(timeout_seconds=10)\n"
    "async def double(n: int) -> int:\n"
    "    return n * 2\n"
)


def _tar(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for path, data in files.items():
            info = tarfile.TarInfo(path)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.mark.asyncio
async def test_service_lifecycle(client) -> None:
    created = await client.post(
        "/v1/services",
        json={"name": "e2e-app", "description": "e2e lifecycle app"},
    )
    assert created.status_code == 201, created.text
    s = created.json()
    try:
        r1 = await client.post(
            f"/v1/services/{s['id']}/versions",
            content=_tar({"index.html": b"<h1>one</h1>", "functions.py": FN.encode()}),
            headers={"content-type": "application/gzip"},
        )
        assert r1.status_code == 201, r1.text

        page = await client.get("/svc/e2e-app/")
        assert page.status_code == 200 and "one" in page.text

        fn = await client.post(
            "/svc/e2e-app/_gateway/functions/double", json={"n": 21}
        )
        assert fn.status_code == 200, fn.text
        assert fn.json() == 42

        r2 = await client.post(
            f"/v1/services/{s['id']}/versions",
            content=_tar({"index.html": b"<h1>two</h1>"}),
            headers={"content-type": "application/gzip"},
        )
        assert r2.status_code == 201, r2.text
        assert "two" in (await client.get("/svc/e2e-app/")).text

        versions = (await client.get(f"/v1/services/{s['id']}/versions")).json()
        v1_id = [v["id"] for v in versions["items"] if v["version"] == 1][0]
        rb = await client.post(
            f"/v1/services/{s['id']}/_activate", json={"version_id": v1_id}
        )
        assert rb.status_code == 200, rb.text
        assert "one" in (await client.get("/svc/e2e-app/")).text

        client_js = await client.get("/svc/_client/primer.js")
        assert client_js.status_code == 200
        assert "Primer" in client_js.text
    finally:
        await client.delete(f"/v1/services/{s['id']}")


@pytest.mark.asyncio
async def test_service_publish_bundle_over_mcp(client, base_url, unique_suffix):
    """A discovered binary tool body reaches publish and serves the original bytes."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client
    from primer.bootstrap.seed import default_policy_id

    tool_id = "services__publish_service_version"
    policy_path = "/v1/tool_approval_policies/" + default_policy_id("services", "publish_service_version")

    async def restore(path, body):
        response = await client.put(path, json=body)
        assert response.status_code in (200, 204), response.text

    async def remove(path):
        response = await client.delete(path)
        assert response.status_code in (200, 204, 404), response.text

    def text(result):
        assert not result.is_error, result
        return "".join(getattr(part, "text", "") for part in result.content)

    # Register every restoration before mutation; all cleanups run even if one fails.
    async with AsyncExitStack() as cleanup:
        exposure = await client.get("/v1/mcp_exposure")
        assert exposure.status_code == 200, exposure.text
        old_exposure = exposure.json()
        cleanup.push_async_callback(restore, "/v1/mcp_exposure", {
            key: old_exposure[key] for key in ("enabled", "mode", "allowed_tools")
        })
        policy = await client.get(policy_path)
        assert policy.status_code == 200, policy.text
        cleanup.push_async_callback(restore, policy_path, policy.json())
        await restore(policy_path, {**policy.json(), "enabled": False})
        await restore("/v1/mcp_exposure", {
            "enabled": True, "mode": "allowlist", "allowed_tools": [tool_id],
        })
        token = await client.post("/v1/auth/tokens", json={
            "name": f"publish-{unique_suffix}", "scopes": ["mcp"],
        })
        assert token.status_code == 201, token.text
        cleanup.push_async_callback(remove, f"/v1/auth/tokens/{token.json()['id']}")
        name = f"mcp-publish-{unique_suffix}"
        service = await client.post("/v1/services", json={
            "name": name, "description": "MCP binary publishing journey",
        })
        assert service.status_code == 201, service.text
        sid = service.json()["id"]
        cleanup.push_async_callback(remove, f"/v1/services/{sid}")
        binary = bytes(range(256))
        bundle = _tar({"index.html": b"published through MCP", "asset.bin": binary})
        async with create_mcp_http_client(
            headers={"Authorization": f"Bearer {token.json()['plaintext']}"},
        ) as http_client:
            async with streamable_http_client(
                f"{base_url}/v1/mcp/", http_client=http_client,
            ) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.call_tool("list_tools", arguments={
                        "text_prefix": tool_id, "include_schema": True,
                    })
                    item, = json.loads(text(listed))["items"]
                    assert item["scoped_id"] == tool_id
                    schema = item["schema"]
                    assert "body" in schema["required"]
                    assert schema["properties"]["body"]["contentEncoding"] == "base64"
                    called = await session.call_tool("call_tool", arguments={
                        "scoped_id": tool_id,
                        "arguments": {"service_id": sid, "body": base64.b64encode(bundle).decode("ascii")},
                    })
                    envelope = json.loads(text(called))
                    assert envelope["ok"] is True, envelope
                    version = json.loads(envelope["output"])
                    assert set(version["files"]) == {"index.html", "asset.bin"}
        saved = await client.get(f"/v1/services/{sid}")
        assert saved.status_code == 200, saved.text
        assert saved.json()["active_version_id"] == version["id"]
        page = await client.get(f"/svc/{name}/")
        assert page.status_code == 200 and page.content == b"published through MCP"
        asset = await client.get(f"/svc/{name}/asset.bin")
        assert asset.status_code == 200 and asset.content == binary
