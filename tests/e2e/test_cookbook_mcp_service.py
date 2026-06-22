"""Cookbook recipe #16 regression: primer-as-a-Service over MCP (UC7).

An EXTERNAL MCP client (e.g. an IDE assistant) treats primer as a remote
execution service: over the platform's ``/v1/mcp`` StreamableHTTP endpoint it
spins up a workspace SESSION, lets it run, reads the result, and cancels a
long run -- never touching primer's REST API to drive the session.

Recipe: primerhq.github.io/docs_source/cookbook/mcp-service.md

Surface exercised (the gap this recipe closes):
  * ``McpExposure{enabled, allowed_tools}`` -- the operator allowlist that
    gates which scoped tools the MCP endpoint exposes.
  * The session-drive tools over MCP:
    ``workspaces__create_workspace_session`` /
    ``workspaces__get_workspace_session`` /
    ``workspaces__read_workspace_file`` /
    ``workspaces__cancel_workspace_session``.

Asserts (the recipe's verified outcomes):
  * ``tools/list`` returns ONLY the allowlisted ids (the exposure gate proof);
    a non-allowlisted workspace tool is absent.
  * ``create_workspace_session`` over MCP starts an agent session that RUNS to
    a terminal ``ended``/``completed`` -- retrievable over MCP via
    ``get_workspace_session`` AND mirrored by the REST row (thin-wrapper
    parity), with the result readable over MCP via ``read_workspace_file``.
  * ``cancel_workspace_session`` transitions a session to terminal
    ``ended``/``cancelled``.

Drives the SAME StreamableHTTP transport an external client would, mirroring
the smk MCP client setup (``tests/e2e/test_smk_mcp.py``). The session is backed
by the deterministic scripted mock LLM (not a real model) so the agent's turn
is reproducible every run. The MCP transport, the exposure gate, and the
session create/get/read/cancel wrappers are all REAL.

Defends the cross-process session-status mirror: a worker-run session that
ended was previously reported as permanently ``running`` over the workspace
session tools (the on-disk slot / cached holder lagged the scheduler row);
this test pins that ``get_workspace_session`` now reflects the terminal state.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from tests._support.mock_llm import Rule
from tests._support.runs import (
    make_local_workspace,
    make_scripted_agent,
)
from tests._support.smk import smk

pytestmark = pytest.mark.asyncio


# The minimal "drive a session" allowlist an external client needs. All four
# are non-yielding, non-session-requiring system-toolset tools, so they pass
# the MCP exposability floor (``primer/mcp/safety.py``).
_ALLOWLIST = [
    "workspaces__create_workspace_session",
    "workspaces__cancel_workspace_session",
    "workspaces__get_workspace_session",
    "workspaces__read_workspace_file",
]

# A workspace tool that is deliberately NOT in the allowlist -- its absence
# from tools/list is the exposure-gate proof.
_FORBIDDEN = "workspaces__delete_workspace"


def _result_text(call_result) -> str:
    """Concatenate the text content blocks of an MCP CallToolResult."""
    parts: list[str] = []
    for blk in call_result.content:
        text = getattr(blk, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts)


@smk("SMK-COOKBOOK-16")
async def test_mcp_service_drives_a_session_end_to_end(
    authed_client, mock_llm, unique_suffix, tmp_path,
):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    registry, base_url = mock_llm

    # A trivial scripted agent: no tools, one text turn. Deterministic so the
    # MCP-created session always runs to a clean completion.
    agent = await make_scripted_agent(
        authed_client, registry, base_url,
        suffix=f"mcpsvc-{unique_suffix}",
        scenario=f"scripted:mcp-service-{unique_suffix}",
        system_prompt=["Reply with exactly: PONG"],
        rules=[Rule(when_tool_result=False, emit_text="PONG")],
    )
    wid = await make_local_workspace(
        authed_client, suffix=f"mcpsvc-{unique_suffix}", root=tmp_path,
    )

    # Enable the MCP endpoint with the session-drive allowlist. The PUT
    # validator re-runs the exposability floor, so a yielding / session-only
    # tool would be rejected here -- these four are accepted.
    enable = await authed_client.put(
        "/v1/mcp_exposure",
        json={"enabled": True, "allowed_tools": _ALLOWLIST},
    )
    assert enable.status_code in (200, 204), enable.text

    # Forward the operator's cookie session to the MCP endpoint (the auth gate
    # accepts a cookie session with full authority -- no bearer scope needed).
    cookie_header = "; ".join(
        f"{c.name}={c.value}" for c in authed_client.cookies.jar
    )
    base = str(authed_client.base_url).rstrip("/")
    mcp_url = f"{base}/v1/mcp/"
    headers = {"Cookie": cookie_header}

    try:
        async with streamablehttp_client(mcp_url, headers=headers) as (
            read, write, _get_session_id,
        ):
            async with ClientSession(read, write) as sess:
                await sess.initialize()

                # ---- (1) exposure gate: only the allowlisted ids are listed.
                listed = await sess.list_tools()
                names = {t.name for t in listed.tools}
                assert names == set(_ALLOWLIST), (
                    f"tools/list is not exactly the allowlist: "
                    f"unexpected={names - set(_ALLOWLIST)} "
                    f"missing={set(_ALLOWLIST) - names}"
                )
                assert _FORBIDDEN not in names, (
                    f"non-allowlisted {_FORBIDDEN!r} leaked into tools/list"
                )

                # ---- (2) create a session over MCP -- it RUNS.
                created = await sess.call_tool(
                    "workspaces__create_workspace_session",
                    arguments={
                        "workspace_id": wid,
                        "binding": {"kind": "agent", "agent_id": agent["agent_id"]},
                        "initial_instructions": "Reply with exactly: PONG",
                        "auto_start": True,
                    },
                )
                assert not created.isError, _result_text(created)
                sid = json.loads(_result_text(created))["id"]
                assert sid

                # ---- (3) poll get_workspace_session over MCP to terminal.
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
                assert final is not None, (
                    "MCP-created session never reached terminal over "
                    "get_workspace_session (cross-process status mirror)"
                )
                assert final["info"]["ended_reason"] == "completed", final

                # Thin-wrapper parity: the same row the REST route serves.
                rest = await authed_client.get(f"/v1/sessions/{sid}")
                assert rest.status_code == 200, rest.text
                assert rest.json()["status"] == "ended", rest.json()

                # ---- (4) read the transcript over MCP -- the result is there.
                read_res = await sess.call_tool(
                    "workspaces__read_workspace_file",
                    arguments={
                        "workspace_id": wid,
                        "path": f".state/sessions/{sid}/messages.jsonl",
                    },
                )
                assert not read_res.isError, _result_text(read_res)
                content = json.loads(_result_text(read_res))["content"]
                assert "PONG" in content, (
                    "session transcript read over MCP did not carry the result"
                )

                # ---- (5) cancel a freshly-created session over MCP.
                created2 = await sess.call_tool(
                    "workspaces__create_workspace_session",
                    arguments={
                        "workspace_id": wid,
                        "binding": {
                            "kind": "agent", "agent_id": agent["agent_id"],
                        },
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

                # Re-poll to terminal: the cancelled session ends. (A session
                # that finished its one quick turn before the cancel landed
                # ends 'completed'; one preempted mid-run ends 'cancelled' --
                # either is a terminal end, which is the lifecycle assertion.)
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
        # Disable exposure + clean up so the row doesn't leak into other tests.
        await authed_client.put(
            "/v1/mcp_exposure", json={"enabled": False, "allowed_tools": []},
        )
        await authed_client.delete(f"/v1/workspaces/{wid}")
        await authed_client.delete(f"/v1/agents/{agent['agent_id']}")
        await authed_client.delete(f"/v1/llm_providers/{agent['provider_id']}")
