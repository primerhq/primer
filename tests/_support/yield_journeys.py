"""Helpers for driving REAL yielding-tool park/resume cycles in e2e.

The legacy journeys seeded park state by asyncpg-injecting onto the
session row plus a now-deleted ``session_leases`` row. The active
ClaimEngine path holds its lease in the engine (in-memory for the
sqlite/in-process bus), so a DB-only injection can never re-arm it.

Instead these helpers drive the genuine path end to end: a scripted
mock-LLM agent emits a yielding tool call, the session-dispatch /
ClaimEngine path runs the turn, the tool yields, the engine parks
(drops the lease + writes the park columns). The operator then
responds / cancels / the row times out, the YieldEventListener flips
parked -> resumable and re-arms the engine lease, and the engine
resumes the session and advances the turn.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from tests._support.mock_llm import Rule, ScriptRegistry
from tests._support.runs import (
    make_local_workspace,
    make_scripted_agent,
    start_agent_session,
)


async def drive_park_on_tool(
    client: httpx.AsyncClient,
    registry: ScriptRegistry,
    mock_base_url: str,
    *,
    suffix: str,
    tool: str,
    tool_args: dict,
    root: Path,
    park_timeout_s: float = 30.0,
    interval_s: float = 0.25,
) -> tuple[str, str, dict]:
    """Run a real turn until the session parks on ``tool``.

    Builds a scripted agent whose first turn emits a single tool call
    to ``tool`` (a yielding tool, e.g. ``system__ask_user``) and, after
    the tool result comes back on resume, emits a terminating text
    reply. Starts the session and polls until ``parked_status`` is
    ``parked``.

    Returns ``(session_id, scenario, parked_body)``.
    """
    scenario = f"scripted:{suffix}"
    bare = tool.split("__", 1)[-1]
    agent = await make_scripted_agent(
        client, registry, mock_base_url,
        suffix=suffix, scenario=scenario, tools=[tool],
        rules=[
            Rule(when_tool_offered=bare, when_tool_result=False,
                 emit_tool=tool, emit_args=tool_args),
            Rule(when_tool_result=True, emit_text="done"),
        ],
    )
    wid = await make_local_workspace(client, suffix=suffix, root=root)
    sid = await start_agent_session(
        client, workspace_id=wid, agent_id=agent["agent_id"],
    )

    deadline = asyncio.get_event_loop().time() + park_timeout_s
    last: dict = {}
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/v1/sessions/{sid}")
        if r.status_code == 200:
            last = r.json()
            if last.get("parked_status") == "parked":
                return sid, scenario, last
            if last.get("status") in ("ended",):
                raise AssertionError(
                    f"session {sid} ended before parking on {tool!r}: "
                    f"reason={last.get('ended_reason')!r} body={last!r}"
                )
        await asyncio.sleep(interval_s)
    raise AssertionError(
        f"session {sid} never parked on {tool!r} within "
        f"{park_timeout_s}s; last_body={last!r}"
    )


async def wait_for_resume(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    timeout_s: float = 30.0,
    interval_s: float = 0.5,
    min_turn_no: int | None = None,
) -> dict:
    """Poll GET /v1/sessions/{id} until parked_status is None.

    When ``min_turn_no`` is provided the success condition is
    ``parked_status is None AND turn_no >= min_turn_no`` (BOTH must
    hold). This closes the race where the row is observed mid-resume
    before the turn counter has been written. When ``min_turn_no`` is
    None the original behaviour is preserved (only parked_status is
    checked).
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    last_body: dict = {}
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/v1/sessions/{session_id}")
        if r.status_code == 200:
            last_body = r.json()
            parked_clear = last_body.get("parked_status") in (None, "null")
            if parked_clear:
                if min_turn_no is None:
                    return last_body
                if last_body.get("turn_no", 0) >= min_turn_no:
                    return last_body
        await asyncio.sleep(interval_s)
    last_turn = last_body.get("turn_no")
    raise AssertionError(
        f"session {session_id} did not finish resuming within "
        f"{timeout_s}s; last_turn_no={last_turn!r}; "
        f"last_body={last_body!r}"
    )
