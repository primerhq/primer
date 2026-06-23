"""Cookbook recipe (CLI path): primer-as-a-Service over MCP.

The ``primectl``-driven sibling of ``test_cookbook_mcp_service``. The recipe has
two surfaces, and this test keeps them apart exactly as the migrated doc does:

  * The OPERATOR SETUP (the agent + the workspace) is built with the published
    CLI path: ``primectl create -f`` the scripted LLM provider, the agent, the
    local workspace provider + template, and ``primectl create workspace``.
  * The MCP EXPOSURE enable + allowlist is an operator-only, CONSOLE-driven
    surface: ``PUT /v1/mcp_exposure`` rejects bearer tokens by design
    (``mcp_exposure_cookie_only``), so it is driven over the cookie session, not
    the CLI. This is intentional, not a residual CLI gap.
  * The RUNTIME DRIVE (create the session, poll it, read it, cancel it) is the
    PRODUCT SURFACE: an external MCP client over the StreamableHTTP transport,
    the caller the recipe enables. It is NOT a primectl step; it is simulated
    with a real MCP client, exactly as the API test does.

Asserts the recipe's verified outcomes: the exposure gate (tools/list is exactly
the allowlist), an MCP-created session runs to terminal completed (mirrored by
the CLI's ``get session`` view), the result is readable over MCP, and a cancel
converges to ended.

Recipe: primerhq.github.io/docs_source/cookbook/mcp-service.md
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tests._support.mock_llm import Rule
from tests._support.primectl_driver import Primectl, manifest, mint_token
from tests._support.smk import smk

pytestmark = pytest.mark.asyncio


_ALLOWLIST = [
    "workspaces__create_workspace_session",
    "workspaces__cancel_workspace_session",
    "workspaces__get_workspace_session",
    "workspaces__read_workspace_file",
]
_FORBIDDEN = "workspaces__delete_workspace"


def _result_text(call_result) -> str:
    parts: list[str] = []
    for blk in call_result.content:
        text = getattr(blk, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts)


@smk("SMK-COOKBOOK-CLI-16")
async def test_mcp_service_cli(authed_client, base_url, mock_llm, unique_suffix, tmp_path):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    registry, mock_base_url = mock_llm
    sfx = unique_suffix
    pc = Primectl(base_url, mint_token(base_url, name=f"cli-mcpsvc-{sfx}"))

    pid = f"p-mcpsvc-cli-{sfx}"
    aid = f"a-mcpsvc-cli-{sfx}"
    wp = f"wp-mcpsvc-cli-{sfx}"
    tpl = f"tpl-mcpsvc-cli-{sfx}"
    scenario = f"scripted:mcpsvc-cli-{sfx}"
    registry.register(scenario, [Rule(when_tool_result=False, emit_text="PONG")])

    wid: str | None = None
    try:
        # ---- OPERATOR SETUP over the published CLI path -------------------
        pc.run("create", "-f", manifest(tmp_path, "llm", "llm_provider", {
            "id": pid, "provider": "openchat",
            "models": [{"name": scenario, "context_length": 8192}],
            "config": {"url": mock_base_url, "flavor": "lmstudio"},
            "limits": {"max_concurrency": 4},
        }))
        pc.run("create", "-f", manifest(tmp_path, "agent", "agent", {
            "id": aid, "description": "Trivial PONG agent for MCP offload.",
            "model": {"provider_id": pid, "model_name": scenario},
            "system_prompt": ["Reply with exactly: PONG"],
        }))
        pc.run("create", "-f", manifest(tmp_path, "wp", "workspace_provider", {
            "id": wp, "provider": "local",
            "config": {"kind": "local", "root_path": str(tmp_path)},
        }))
        pc.run("create", "-f", manifest(tmp_path, "tpl", "workspace_template", {
            "id": tpl, "description": "mcp svc cli", "provider_id": wp,
            "backend": {"kind": "local"},
        }))
        wid = pc.run("create", "workspace", "--set", f"template_id={tpl}").stdout.split("/")[1].split()[0]
        assert wid

        # ---- MCP exposure enable: CONSOLE-ONLY surface (cookie session) ----
        # The PUT rejects bearer tokens by design; drive it over the cookie
        # session, mirroring how the console does it. There is no first-class
        # CLI verb for this, and there cannot be: the allowlist is intentionally
        # not bearer-editable.
        enable = await authed_client.put(
            "/v1/mcp_exposure",
            json={"enabled": True, "allowed_tools": _ALLOWLIST},
        )
        assert enable.status_code in (200, 204), enable.text

        # ---- RUNTIME DRIVE: the external MCP client (PRODUCT SURFACE) ------
        cookie_header = "; ".join(
            f"{c.name}={c.value}" for c in authed_client.cookies.jar
        )
        mcp_url = f"{str(authed_client.base_url).rstrip('/')}/v1/mcp/"
        headers = {"Cookie": cookie_header}

        async with streamablehttp_client(mcp_url, headers=headers) as (read, write, _gsi):
            async with ClientSession(read, write) as sess:
                await sess.initialize()

                # (1) exposure gate: only the allowlisted ids are listed.
                listed = await sess.list_tools()
                names = {t.name for t in listed.tools}
                assert names == set(_ALLOWLIST), (
                    f"tools/list is not exactly the allowlist: "
                    f"unexpected={names - set(_ALLOWLIST)} "
                    f"missing={set(_ALLOWLIST) - names}"
                )
                assert _FORBIDDEN not in names

                # (2) create a session over MCP; it RUNS.
                created = await sess.call_tool(
                    "workspaces__create_workspace_session",
                    arguments={
                        "workspace_id": wid,
                        "binding": {"kind": "agent", "agent_id": aid},
                        "initial_instructions": "Reply with exactly: PONG",
                        "auto_start": True,
                    },
                )
                assert not created.isError, _result_text(created)
                sid = json.loads(_result_text(created))["id"]
                assert sid

                # (3) poll get_workspace_session over MCP to terminal.
                final = None
                for _ in range(120):
                    got = await sess.call_tool(
                        "workspaces__get_workspace_session",
                        arguments={"workspace_id": wid, "session_id": sid},
                    )
                    assert not got.isError, _result_text(got)
                    body = json.loads(_result_text(got))
                    info = body.get("info", {})
                    if body.get("status") == "ended" or info.get("status") == "ended":
                        final = body
                        break
                    await asyncio.sleep(0.5)
                assert final is not None, "MCP-created session never reached terminal"
                assert final["info"]["ended_reason"] == "completed", final

                # Parity: the CLI's own session view shows the same terminal row.
                cli_row = pc.run("get", "session", sid, "-r", "-o", "json").json()
                assert cli_row.get("status") == "ended", cli_row

                # (4) read the transcript over MCP; the result is there.
                read_res = await sess.call_tool(
                    "workspaces__read_workspace_file",
                    arguments={
                        "workspace_id": wid,
                        "path": f".state/sessions/{sid}/messages.jsonl",
                    },
                )
                assert not read_res.isError, _result_text(read_res)
                content = json.loads(_result_text(read_res))["content"]
                assert "PONG" in content, "transcript read over MCP missing the result"

                # (5) cancel a freshly-created session over MCP.
                created2 = await sess.call_tool(
                    "workspaces__create_workspace_session",
                    arguments={
                        "workspace_id": wid,
                        "binding": {"kind": "agent", "agent_id": aid},
                        "initial_instructions": "Reply with exactly: PONG",
                        "auto_start": True,
                    },
                )
                assert not created2.isError, _result_text(created2)
                sid2 = json.loads(_result_text(created2))["id"]
                cancelled = await sess.call_tool(
                    "workspaces__cancel_workspace_session",
                    arguments={"workspace_id": wid, "session_id": sid2},
                )
                assert not cancelled.isError, _result_text(cancelled)

                term = None
                for _ in range(120):
                    got = await sess.call_tool(
                        "workspaces__get_workspace_session",
                        arguments={"workspace_id": wid, "session_id": sid2},
                    )
                    body = json.loads(_result_text(got))
                    info = body.get("info", {})
                    if body.get("status") == "ended" or info.get("status") == "ended":
                        term = body
                        break
                    await asyncio.sleep(0.5)
                assert term is not None, "cancelled session never reached ended"
                assert term["info"]["ended_reason"] in ("cancelled", "completed"), term
    finally:
        await authed_client.put(
            "/v1/mcp_exposure", json={"enabled": False, "allowed_tools": []},
        )
        if wid is not None:
            pc.run("delete", "workspace", wid, check=False)
        pc.run("delete", "agent", aid, check=False)
        pc.run("delete", "workspace_template", tpl, check=False)
        pc.run("delete", "workspace_provider", wp, check=False)
        pc.run("delete", "llm_provider", pid, check=False)
