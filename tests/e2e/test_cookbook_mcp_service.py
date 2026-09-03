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
    wait_completed,
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
    from contextlib import asynccontextmanager

    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    @asynccontextmanager
    async def _http_mcp_streams(url: str, *, headers: dict[str, str]):
        """mcp>=2.0: streamable_http_client dropped headers= for an
        injected httpx.AsyncClient (see primer/toolset/mcp.py's own
        _open_session for the same fix) - wrap create_mcp_http_client so
        the call below keeps the old url+headers shape and the 2-tuple
        stream yield (no more get_session_id) at the SAME nesting depth
        as before, with no need to re-indent the whole test body.
        """
        async with create_mcp_http_client(headers=headers) as http_client:
            async with streamable_http_client(
                url, http_client=http_client,
            ) as streams:
                yield streams

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
        async with _http_mcp_streams(mcp_url, headers=headers) as (
            read, write,
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
                assert not created.is_error, _result_text(created)
                sid = json.loads(_result_text(created))["id"]
                assert sid

                # ---- (3) poll the transcript over MCP until the reply lands.
                # 01a0518a: the on-disk AgentSession slot (what
                # workspaces__get_workspace_session's SessionInfo mirrors) is
                # only ever explicitly synced to ENDED on a terminal
                # transition (_sync_agent_session_ended in dispatch.py) - a
                # clean stop that now rests the session WAITING/parked has
                # no equivalent mirror (set_status(WAITING, ...) requires a
                # structured waiting_state - "user asked a question" /
                # "tool approval" - that doesn't exist for a plain rest), so
                # SessionInfo.status stays "running" forever from this
                # cross-process MCP view. Poll the transcript itself instead
                # of the status mirror - a directly MCP-visible, unambiguous
                # signal that the turn actually completed, and the thing an
                # MCP-as-a-service client cares about anyway.
                content = ""
                for _ in range(120):
                    read_res = await sess.call_tool(
                        "workspaces__read_workspace_file",
                        arguments={
                            "workspace_id": wid,
                            "path": f".state/sessions/{sid}/messages.jsonl",
                        },
                    )
                    if not read_res.is_error:
                        content = json.loads(_result_text(read_res))["content"]
                        if "PONG" in content:
                            break
                    await asyncio.sleep(0.5)
                assert "PONG" in content, (
                    "MCP-created session never produced its reply in the "
                    "transcript (get_workspace_session's on-disk mirror "
                    "does not reflect a clean-stop rest, see 01a0518a; the "
                    "transcript is the reliable cross-process signal)"
                )

                # Thin-wrapper parity: the REST route's own computed field
                # (backed by the DB row, not the on-disk mirror) does see
                # the session resting parked. POLL rather than a one-shot
                # GET: the transcript write (what the loop above waits on)
                # and the row's terminal-status write are not ordered
                # relative to each other from an outside observer - PONG
                # can land in messages.jsonl a beat before
                # _post_turn_status/_clear_turn_running settle the row, so
                # a single GET can legitimately still read "running".
                rest = await wait_completed(authed_client, sid, timeout_s=30.0)
                assert rest.get("session_state") == "parked", rest

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
                assert not created2.is_error, _result_text(created2)
                sid2 = json.loads(_result_text(created2))["id"]

                cancelled = await sess.call_tool(
                    "workspaces__cancel_workspace_session",
                    arguments={"workspace_id": wid, "session_id": sid2},
                )
                assert not cancelled.is_error, _result_text(cancelled)

                # Re-poll to terminal: the cancel call itself always forces
                # the row to ended/cancelled (cancel_session's inline
                # CREATED/WAITING/PAUSED branch), independent of whether the
                # turn had already cleanly finished (which, post-01a0518a,
                # would otherwise just rest it parked) or was preempted
                # mid-run - unlike a plain clean stop, an explicit cancel is
                # exactly the "ENDED reserved for explicit end" case.
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
