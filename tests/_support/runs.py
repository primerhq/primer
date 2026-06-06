"""Helpers for run-based SMK tests against the live e2e server.

Build a scripted agent (LLM provider + agent whose model name is the mock
scenario id), materialise a local workspace, start a session, and poll it to
a terminal state. Used by the Phase 1+ run-based tests.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from tests._support.mock_llm import Rule, ScriptRegistry

_TERMINAL = {"ended"}


async def make_scripted_agent(
    client: httpx.AsyncClient,
    registry: ScriptRegistry,
    mock_base_url: str,
    *,
    suffix: str,
    scenario: str,
    rules: list[Rule],
    tools: list[str] | None = None,
    system_prompt: list[str] | None = None,
) -> dict:
    """Register the script, create an OpenChat provider + agent; return ids.

    The agent's model name IS the scenario id, so the mock keys its rules off
    the request model. The provider lists that scenario id as its only model.
    """
    registry.register(scenario, rules)
    pid = f"p-{suffix}"
    aid = f"a-{suffix}"
    pr = await client.post(
        "/v1/llm_providers",
        json={
            "id": pid,
            "provider": "openchat",
            "models": [{"name": scenario, "context_length": 8192}],
            "config": {"url": mock_base_url, "flavor": "lmstudio"},
            "limits": {"max_concurrency": 4},
        },
    )
    assert pr.status_code in (200, 201), pr.text
    ar = await client.post(
        "/v1/agents",
        json={
            "id": aid,
            "description": "smk scripted agent",
            "model": {"provider_id": pid, "model_name": scenario},
            "tools": tools or [],
            "system_prompt": system_prompt or ["You are a scripted test agent."],
        },
    )
    assert ar.status_code in (200, 201), ar.text
    return {"provider_id": pid, "agent_id": aid, "model": scenario}


async def make_local_workspace(
    client: httpx.AsyncClient, *, suffix: str, root: Path
) -> str:
    wp = f"wp-{suffix}"
    tpl = f"tpl-{suffix}"
    rp = await client.post(
        "/v1/workspace_providers",
        json={"id": wp, "provider": "local", "config": {"kind": "local", "root_path": str(root)}},
    )
    assert rp.status_code in (200, 201), rp.text
    rt = await client.post(
        "/v1/workspace_templates",
        json={"id": tpl, "description": "smk", "provider_id": wp, "backend": {"kind": "local"}},
    )
    assert rt.status_code in (200, 201), rt.text
    rw = await client.post("/v1/workspaces", json={"template_id": tpl})
    assert rw.status_code in (200, 201), rw.text
    return rw.json()["id"]


async def make_graph(
    client: httpx.AsyncClient, *, suffix: str, nodes: list[dict], edges: list[dict],
    max_iterations: int | None = None,
) -> str:
    gid = f"g-{suffix}"
    body: dict = {"id": gid, "description": "smk graph", "nodes": nodes, "edges": edges}
    if max_iterations is not None:
        body["max_iterations"] = max_iterations
    r = await client.post("/v1/graphs", json=body)
    assert r.status_code in (200, 201), r.text
    return gid


async def start_graph_session(
    client: httpx.AsyncClient,
    *,
    workspace_id: str,
    graph_id: str,
    instructions: str = "go",
    auto_start: bool = True,
) -> str:
    r = await client.post(
        f"/v1/workspaces/{workspace_id}/sessions",
        json={
            "binding": {"kind": "graph", "graph_id": graph_id},
            "initial_instructions": instructions,
            "auto_start": auto_start,
        },
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


async def start_agent_session(
    client: httpx.AsyncClient,
    *,
    workspace_id: str,
    agent_id: str,
    instructions: str = "go",
    auto_start: bool = True,
) -> str:
    r = await client.post(
        f"/v1/workspaces/{workspace_id}/sessions",
        json={
            "binding": {"kind": "agent", "agent_id": agent_id},
            "initial_instructions": instructions,
            "auto_start": auto_start,
        },
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


async def wait_terminal(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    timeout_s: float = 60.0,
    interval_s: float = 0.5,
) -> dict:
    iters = max(1, int(timeout_s / interval_s))
    last: dict = {}
    for _ in range(iters):
        resp = await client.get(f"/v1/sessions/{session_id}")
        if resp.status_code == 200:
            last = resp.json()
            if last.get("status") in _TERMINAL:
                return last
        await asyncio.sleep(interval_s)
    return last
