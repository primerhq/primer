"""Fixtures that create reusable entities from the mock LLM + testconfig.

These run against the live e2e server (per-test scoped). They mirror the
entity create-body shapes used by the existing e2e tests (see
tests/e2e/test_session_lifecycle_lmstudio.py).
"""
from __future__ import annotations

import pytest_asyncio


@pytest_asyncio.fixture
async def scripted_provider(client, mock_llm, unique_suffix):
    """Create an OpenChat LLM provider pointing at the mock; return its id.

    The mock serves /v1/chat/completions, so we register an ``openchat``
    provider with ``flavor=lmstudio`` (no api_key required). The model name
    ``scripted:default`` is the catch-all scenario; tests that need their own
    script register additional scenario models on the shared registry and set
    the agent's model to that scenario id.
    """
    _registry, base_url = mock_llm
    pid = f"scripted-{unique_suffix}"
    body = {
        "id": pid,
        "provider": "openchat",
        "models": [{"name": "scripted:default", "context_length": 8192}],
        "config": {"url": base_url, "flavor": "lmstudio"},
        "limits": {"max_concurrency": 4},
    }
    r = await client.post("/v1/llm_providers", json=body)
    assert r.status_code in (200, 201), r.text
    return pid


@pytest_asyncio.fixture
async def local_workspace(client, unique_suffix, tmp_path_factory):
    """Create a local workspace provider + template + workspace; return ids."""
    root = tmp_path_factory.mktemp(f"ws-{unique_suffix}")
    wp = f"wsp-{unique_suffix}"
    tpl = f"tpl-{unique_suffix}"
    rp = await client.post(
        "/v1/workspace_providers",
        json={"id": wp, "provider": "local", "config": {"kind": "local", "root_path": str(root)}},
    )
    assert rp.status_code in (200, 201), rp.text
    rt = await client.post(
        "/v1/workspace_templates",
        json={
            "id": tpl,
            "description": "smk local template",
            "provider_id": wp,
            "backend": {"kind": "local"},
        },
    )
    assert rt.status_code in (200, 201), rt.text
    rw = await client.post("/v1/workspaces", json={"template_id": tpl})
    assert rw.status_code in (200, 201), rw.text
    return {"provider": wp, "template": tpl, "workspace": rw.json()["id"]}
