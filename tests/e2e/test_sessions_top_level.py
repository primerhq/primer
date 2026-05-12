"""E2E: top-level cross-workspace Sessions surface.

Covers backlog item T0042. Spec §12 says `GET /v1/sessions/{id}` reads
a Session row without a workspace prefix, which is useful when the
caller doesn't know which workspace owns the session.

Setup chain: LLMProvider → Agent → WorkspaceProvider → WorkspaceTemplate
→ Workspace → Session (binding=agent, no auto_start). Then GET via
both the workspace-scoped and the top-level endpoint and assert basic
identity fields agree.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest


def _llm_body(entity_id: str) -> dict:
    return {
        "id": entity_id,
        "provider": "anthropic",
        "models": [{"name": "claude-sonnet-4-6", "context_length": 200_000}],
        "config": {"api_key": "sk-test-placeholder"},
        "limits": {"max_concurrency": 1},
    }


def _agent_body(entity_id: str, *, provider_id: str) -> dict:
    return {
        "id": entity_id,
        "description": "test agent",
        "model": {"provider_id": provider_id, "model_name": "claude-sonnet-4-6"},
        "tools": [],
    }


def _workspace_provider_body(entity_id: str, root: Path) -> dict:
    return {
        "id": entity_id,
        "provider": "local",
        "config": {"kind": "local", "path": str(root)},
    }


def _workspace_template_body(entity_id: str, *, provider_id: str) -> dict:
    return {
        "id": entity_id,
        "description": "test template",
        "provider_id": provider_id,
        "backend": {"kind": "local"},
    }


def _session_body(*, agent_id: str) -> dict:
    return {
        "binding": {"kind": "agent", "agent_id": agent_id},
        "auto_start": False,
    }


@pytest.mark.asyncio
async def test_t0042_top_level_get_session_works_without_workspace_context(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    provider_id = f"llm-{unique_suffix}"
    agent_id = f"agent-{unique_suffix}"
    wp_id = f"wp-{unique_suffix}"
    tpl_id = f"wt-{unique_suffix}"
    workspace_id: str | None = None
    session_id: str | None = None

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    try:
        ag = await client.post(
            "/v1/agents", json=_agent_body(agent_id, provider_id=provider_id),
        )
        assert ag.status_code == 201, ag.text
        try:
            wp = await client.post(
                "/v1/workspace_providers",
                json=_workspace_provider_body(wp_id, tmp_path),
            )
            assert wp.status_code == 201, wp.text
            try:
                tpl = await client.post(
                    "/v1/workspace_templates",
                    json=_workspace_template_body(tpl_id, provider_id=wp_id),
                )
                assert tpl.status_code == 201, tpl.text
                try:
                    ws = await client.post(
                        "/v1/workspaces",
                        json={"template_id": tpl_id},
                    )
                    assert ws.status_code == 201, ws.text
                    workspace_id = ws.json()["id"]

                    sess = await client.post(
                        f"/v1/workspaces/{workspace_id}/sessions",
                        json=_session_body(agent_id=agent_id),
                    )
                    assert sess.status_code == 201, sess.text
                    session_row = sess.json()
                    session_id = session_row["id"]
                    assert session_row["workspace_id"] == workspace_id

                    # Top-level GET — no workspace prefix in the URL
                    top = await client.get(f"/v1/sessions/{session_id}")
                    assert top.status_code == 200, top.text
                    top_body = top.json()
                    assert top_body["id"] == session_id
                    assert top_body["workspace_id"] == workspace_id
                    # Binding identity is preserved
                    assert top_body["binding"]["kind"] == "agent"
                    assert top_body["binding"]["agent_id"] == agent_id

                    # Top-level lookup of a missing id is a clean 404
                    missing = await client.get(
                        f"/v1/sessions/missing-{unique_suffix}",
                    )
                    assert missing.status_code == 404, missing.text
                    assert missing.json()["type"] == "/errors/not-found"
                finally:
                    if workspace_id is not None:
                        await client.delete(f"/v1/workspaces/{workspace_id}")
                    await client.delete(f"/v1/workspace_templates/{tpl_id}")
            finally:
                await client.delete(f"/v1/workspace_providers/{wp_id}")
        finally:
            await client.delete(f"/v1/agents/{agent_id}")
    finally:
        await client.delete(f"/v1/llm_providers/{provider_id}")
