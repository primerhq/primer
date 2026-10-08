"""Seeding for journeys that need a session driven by the scripted mock LLM: a provider, a local workspace and an agent, then sessions.

The model name is unique per call (``scripted:<prefix>-<suffix>``), so a journey registers its rules under ``ids["model_name"]``
without touching another journey's.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from tests._support.model_profiles import agent_model, seed_llm_provider_with


def seed_scripted_agent(base_url: str, mock_base_url: str, suffix: str, tmp_path: Path, *, prefix: str = "sa") -> dict:
    ids = {"llm": f"{prefix}-llm-{suffix}", "wp": f"{prefix}-wp-{suffix}", "tpl": f"{prefix}-tpl-{suffix}", "agent": f"{prefix}-ag-{suffix}"}
    model_name = f"scripted:{prefix}-{suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = seed_llm_provider_with(c, {
            "id": ids["llm"], "provider": "openchat",
            "models": [{"name": model_name, "context_length": 131_072}],
            "config": {"url": mock_base_url, "flavor": "other"},
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201, f"seed llm failed: {r.status_code} {r.text}"
        r = c.post("/v1/workspace_providers", json={
            "id": ids["wp"], "provider": "local", "config": {"kind": "local", "root_path": str(tmp_path)},
        })
        assert r.status_code == 201, f"seed wp failed: {r.status_code} {r.text}"
        r = c.post("/v1/workspace_templates", json={
            "id": ids["tpl"], "description": "scripted session journey", "provider_id": ids["wp"], "backend": {"kind": "local"},
        })
        assert r.status_code == 201, f"seed tpl failed: {r.status_code} {r.text}"
        r = c.post("/v1/workspaces", json={"template_id": ids["tpl"]})
        assert r.status_code == 201, f"seed workspace failed: {r.status_code} {r.text}"
        ids["workspace"] = r.json()["id"]
        r = c.post("/v1/agents", json={
            "id": ids["agent"], "description": "scripted session journey agent",
            "model": agent_model(ids["llm"], model_name), "tools": [],
        })
        assert r.status_code == 201, f"seed agent failed: {r.status_code} {r.text}"
    ids["model_name"] = model_name
    return ids


def start_session(client: httpx.Client, ids: dict, instructions: str, *, auto_start: bool) -> str:
    r = client.post(f"/v1/workspaces/{ids['workspace']}/sessions", json={
        "binding": {"kind": "agent", "agent_id": ids["agent"]}, "initial_instructions": instructions, "auto_start": auto_start,
    })
    assert r.status_code == 201, f"create session failed: {r.status_code} {r.text}"
    return r.json()["id"]
