"""CI-owned real MCP SDK journey for the Tools Universal M2 surface.

The e2e conftest collect-ignores this module unless ``PRIMER_RUN_E2E=1``.
It is intentionally not run in the resource-constrained local lane.
"""

from __future__ import annotations

import json

import pytest


pytestmark = pytest.mark.asyncio

_META_TOOLS = {"list_tools", "find_tools", "call_tool"}
_GENERATED_READ = "auth__auth_status"


def _text(result) -> str:
    return "".join(getattr(part, "text", "") for part in result.content)


async def test_real_sdk_discovers_and_calls_a_generated_read(
    client, base_url: str, unique_suffix: str,
) -> None:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    before = (await client.get("/v1/mcp_exposure")).json()
    token = await client.post(
        "/v1/auth/tokens",
        json={"name": f"m2-sdk-{unique_suffix}", "scopes": ["mcp"]},
    )
    assert token.status_code == 201, token.text
    secret = token.json()["plaintext"]

    enabled = await client.put(
        "/v1/mcp_exposure",
        json={
            "enabled": True,
            "mode": "allowlist",
            "allowed_tools": [_GENERATED_READ],
        },
    )
    assert enabled.status_code in (200, 204), enabled.text

    try:
        async with create_mcp_http_client(
            headers={"Authorization": f"Bearer {secret}"},
        ) as http_client:
            async with streamable_http_client(
                f"{base_url}/v1/mcp/", http_client=http_client,
            ) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    advertised = await session.list_tools()
                    assert {tool.name for tool in advertised.tools} == _META_TOOLS

                    listed = await session.call_tool(
                        "list_tools",
                        arguments={
                            "text_prefix": _GENERATED_READ,
                            "include_schema": True,
                        },
                    )
                    assert not listed.is_error, _text(listed)
                    catalogue = json.loads(_text(listed))
                    assert [item["scoped_id"] for item in catalogue["items"]] == [
                        _GENERATED_READ,
                    ]
                    assert catalogue["items"][0]["schema"]["type"] == "object"

                    called = await session.call_tool(
                        "call_tool",
                        arguments={"scoped_id": _GENERATED_READ, "arguments": {}},
                    )
                    assert not called.is_error, _text(called)
                    envelope = json.loads(_text(called))
                    assert envelope["ok"] is True, envelope
                    status = json.loads(envelope["output"])
                    assert status["authenticated"] is True
    finally:
        restored = await client.put(
            "/v1/mcp_exposure",
            json={
                "enabled": before["enabled"],
                "mode": before.get("mode", "allowlist"),
                "allowed_tools": before.get("allowed_tools", []),
            },
        )
        assert restored.status_code in (200, 204), restored.text
