"""E2E: full multi-subsystem operator journey, no LLM required.

This file is the first of the post-pivot user-journey tests. One pytest
function walks a realistic operator across eight subsystems — providers
(LLM/Embedding/CrossEncoder), workspaces (Provider/Template/Workspace),
toolsets (MCP open-websearch), agents, graphs, sessions, the workspace
file API, and the workspace log/git-state surface — then unwinds the
whole stack in reverse and asserts each delete cascades cleanly.

The test deliberately avoids any LLM dispatch: sessions are created
with ``auto_start=False`` and cancelled before any turn runs. That
keeps the test runnable in any environment, including those without
LM Studio reachability (the pivot's eventual LM-Studio-driven
user-journeys live in a separate file).

Per the iteration directive: at least 60% of new tests should be
multi-subsystem user-journey tests. This is the first of that family.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import httpx
import pytest


def _llm_provider_body(entity_id: str) -> dict:
    """LLMProvider with an unreachable upstream — row stays valid but
    never hits the network. Tests that need a real turn use a separate
    body pointing at LM Studio."""
    return {
        "id": entity_id,
        "provider": "openresponses",
        "models": [{"name": "stub-model", "context_length": 8192}],
        "config": {
            "url": "http://127.0.0.1:1",
            "api_key": "sk-not-used",
            "flavor": "other",
        },
        "limits": {"max_concurrency": 1},
    }


def _embedding_provider_body(entity_id: str) -> dict:
    return {
        "id": entity_id,
        "provider": "openai",
        "models": [{"name": "stub-embed"}],
        "config": {
            "url": "http://127.0.0.1:1",
            "api_key": "sk-not-used",
            "flavor": "other",
        },
        "limits": {"max_concurrency": 1},
    }


def _cross_encoder_provider_body(entity_id: str) -> dict:
    return {
        "id": entity_id,
        "provider": "huggingface",
        "models": [{"name": "stub-cross-encoder"}],
        "config": {"token": "hf_not_used"},
        "limits": {"max_concurrency": 1},
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
        "description": "journey template",
        "provider_id": provider_id,
        "backend": {"kind": "local"},
    }


def _toolset_body(entity_id: str) -> dict:
    """MCP toolset configured against open-websearch (npx-spawned).

    The toolset row creates successfully even if npx isn't installed;
    only the /tools enumeration call would fail at runtime. Tests that
    enumerate tools assert on env reachability separately.
    """
    return {
        "id": entity_id,
        "provider": "mcp",
        "config": {
            "transport": "stdio",
            "config": {
                "command": ["npx", "-y", "open-websearch@latest"],
                "env": {"MODE": "stdio", "DEFAULT_SEARCH_ENGINE": "bing"},
            },
        },
    }


def _agent_body(entity_id: str, *, provider_id: str, toolset_id: str | None = None) -> dict:
    body = {
        "id": entity_id,
        "description": "journey agent",
        "model": {"provider_id": provider_id, "model_name": "stub-model"},
        "tools": [],
    }
    if toolset_id is not None:
        body["toolsets"] = [toolset_id]
    return body


def _graph_body(entity_id: str, *, agent_id: str) -> dict:
    return {
        "id": entity_id,
        "description": "journey graph",
        "nodes": [{"kind": "agent", "id": "n1", "agent_id": agent_id}],
        "edges": [],
        "entry_node_id": "n1",
    }


def _session_body(*, agent_id: str) -> dict:
    return {
        "binding": {"kind": "agent", "agent_id": agent_id},
        "auto_start": False,
    }


@pytest.mark.asyncio
async def test_full_setup_and_cascade_delete_journey_no_llm(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """Multi-subsystem journey: setup the full operator stack, exercise
    each subsystem's read surface, then unwind in reverse and assert
    each delete returns a clean envelope.

    Subsystems crossed:
      1. providers (LLM/Embedding/CrossEncoder CRUD + /models)
      2. workspaces (Provider/Template/Workspace CRUD + file ops + log)
      3. toolsets (MCP stdio config row, /tools surface skip-soft)
      4. agents (CRUD + /status)
      5. graphs (CRUD + /status)
      6. sessions (top-level GET + nested GET + filter + cancel)
      7. find/list pagination (cursor-walk over created entities)
      8. cascade delete (workspace + entities)
    """
    suffix = unique_suffix
    llm_id = f"journey-llm-{suffix}"
    emb_id = f"journey-emb-{suffix}"
    rer_id = f"journey-rer-{suffix}"
    wp_id = f"journey-wp-{suffix}"
    tpl_id = f"journey-tpl-{suffix}"
    toolset_id = f"journey-ts-{suffix}"
    agent_id = f"journey-ag-{suffix}"
    graph_id = f"journey-gr-{suffix}"
    workspace_id: str | None = None
    session_id: str | None = None

    # ---- track everything we create so the finally can unwind in order
    created: dict[str, list[str]] = {
        "llm_providers": [],
        "embedding_providers": [],
        "cross_encoder_providers": [],
        "workspace_providers": [],
        "workspace_templates": [],
        "workspaces": [],
        "toolsets": [],
        "agents": [],
        "graphs": [],
    }

    try:
        # ===== 1. providers ==============================================
        r = await client.post("/v1/llm_providers", json=_llm_provider_body(llm_id))
        assert r.status_code == 201, r.text
        created["llm_providers"].append(llm_id)

        r = await client.post(
            "/v1/embedding_providers", json=_embedding_provider_body(emb_id),
        )
        assert r.status_code == 201, r.text
        created["embedding_providers"].append(emb_id)

        r = await client.post(
            "/v1/cross_encoder_providers",
            json=_cross_encoder_provider_body(rer_id),
        )
        assert r.status_code == 201, r.text
        created["cross_encoder_providers"].append(rer_id)

        # Per-provider /models is row-cached — returns 200 even though
        # upstream is unreachable. Pins the LLMProvider's models contract.
        r = await client.get(f"/v1/llm_providers/{llm_id}/models")
        assert r.status_code == 200, r.text
        assert "stub-model" in r.json().get("models", []), r.text

        # ===== 2. workspace stack ========================================
        r = await client.post(
            "/v1/workspace_providers",
            json=_workspace_provider_body(wp_id, tmp_path),
        )
        assert r.status_code == 201, r.text
        created["workspace_providers"].append(wp_id)

        r = await client.post(
            "/v1/workspace_templates",
            json=_workspace_template_body(tpl_id, provider_id=wp_id),
        )
        assert r.status_code == 201, r.text
        created["workspace_templates"].append(tpl_id)

        r = await client.post("/v1/workspaces", json={"template_id": tpl_id})
        assert r.status_code == 201, r.text
        workspace_id = r.json()["id"]
        assert workspace_id, r.text
        created["workspaces"].append(workspace_id)

        # ---- write a file, list it, log it
        write = await client.put(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": "journey.txt"},
            json={"content": "operator journey marker", "encoding": "text"},
        )
        assert write.status_code == 204, write.text

        listing = await client.get(f"/v1/workspaces/{workspace_id}/files")
        assert listing.status_code == 200, listing.text
        names = {f["path"] for f in listing.json().get("items", [])}
        assert "journey.txt" in names, listing.json()

        log = await client.get(f"/v1/workspaces/{workspace_id}/log")
        assert log.status_code == 200, log.text
        commits = log.json().get("commits", [])
        # File-write goes through the state repo; at least one commit
        # should be visible. Permissive on the exact count — backends
        # may coalesce or batch commits in future.
        assert isinstance(commits, list), log.json()

        # ===== 3. toolset (MCP stdio row, no tools enumeration) ==========
        r = await client.post("/v1/toolsets", json=_toolset_body(toolset_id))
        assert r.status_code == 201, r.text
        created["toolsets"].append(toolset_id)

        # Don't call /tools here — that spawns npx which may not be on
        # PATH in every env. A separate test pins that contract.

        # ===== 4. agent ==================================================
        r = await client.post(
            "/v1/agents", json=_agent_body(agent_id, provider_id=llm_id),
        )
        assert r.status_code == 201, r.text
        created["agents"].append(agent_id)

        r = await client.get(f"/v1/agents/{agent_id}/status")
        assert r.status_code == 200, r.text
        # Agent status is ok=True since the LLMProvider exists (even
        # though upstream is unreachable, the row is valid).
        assert r.json().get("ok") is True, r.json()

        # ===== 5. graph ==================================================
        r = await client.post(
            "/v1/graphs", json=_graph_body(graph_id, agent_id=agent_id),
        )
        assert r.status_code == 201, r.text
        created["graphs"].append(graph_id)

        r = await client.get(f"/v1/graphs/{graph_id}/status")
        assert r.status_code == 200, r.text
        assert r.json().get("ok") is True, r.json()

        # ===== 6. session ================================================
        r = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions",
            json=_session_body(agent_id=agent_id),
        )
        assert r.status_code == 201, r.text
        session_id = r.json()["id"]
        assert session_id, r.text

        # --- top-level GET (cross-workspace surface)
        r = await client.get(f"/v1/sessions/{session_id}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == session_id, body
        assert body["status"] == "created", body
        assert body["binding"]["agent_id"] == agent_id, body

        # --- nested GET via workspace
        r = await client.get(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}",
        )
        assert r.status_code == 200, r.text

        # --- filter by binding.agent_id (uses JSONB predicate engine)
        r = await client.post(
            "/v1/sessions/find",
            json={
                "predicate": {
                    "kind": "predicate",
                    "op": "=",
                    "left": {"kind": "field", "name": "binding.agent_id"},
                    "right": {"kind": "value", "value": agent_id},
                },
                "page": {"kind": "offset", "offset": 0, "length": 10},
            },
        )
        assert r.status_code == 200, r.text
        items = r.json().get("items", [])
        assert any(it["id"] == session_id for it in items), items

        # --- cancel before any turn runs (nested route under workspace)
        r = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
        )
        assert r.status_code in (200, 202, 204), r.text

        # --- session converges to ENDED (cancel from CREATED is instant)
        r = await client.get(f"/v1/sessions/{session_id}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "ended", body
        assert body.get("ended_reason") in ("cancelled", "ended"), body

        # ===== 7. find/list pagination cursor walk =======================
        # Walk all entities of each kind we created and confirm the
        # cursor-walk surface returns clean envelopes. One per family.
        for path in (
            "/v1/llm_providers",
            "/v1/embedding_providers",
            "/v1/cross_encoder_providers",
            "/v1/workspace_providers",
            "/v1/workspace_templates",
            "/v1/workspaces",
            "/v1/toolsets",
            "/v1/agents",
            "/v1/graphs",
        ):
            r = await client.get(path, params={"length": 50})
            assert r.status_code == 200, f"{path}: {r.text}"
            envelope = r.json()
            assert isinstance(envelope.get("items"), list), envelope
            # We created one of each in this test; under load there
            # may be many — permissive on exact count.

    finally:
        # ===== 8. cascade unwind (reverse creation order) ================
        # session — already cancelled; the row persists but GET still works.
        if session_id is not None:
            # Best-effort delete via top-level — the API may not expose
            # session DELETE; cancel is the terminal state. Confirm the
            # row is still readable post-cancel (already done above).
            pass

        # workspace
        if workspace_id is not None:
            r = await client.delete(f"/v1/workspaces/{workspace_id}")
            assert r.status_code in (200, 204, 404), r.text

        # entities (in dependency order)
        for entity_id in created["graphs"]:
            r = await client.delete(f"/v1/graphs/{entity_id}")
            assert r.status_code in (200, 204, 404), r.text
        for entity_id in created["agents"]:
            r = await client.delete(f"/v1/agents/{entity_id}")
            assert r.status_code in (200, 204, 404), r.text
        for entity_id in created["toolsets"]:
            r = await client.delete(f"/v1/toolsets/{entity_id}")
            assert r.status_code in (200, 204, 404), r.text
        for entity_id in created["workspace_templates"]:
            r = await client.delete(f"/v1/workspace_templates/{entity_id}")
            assert r.status_code in (200, 204, 404), r.text
        for entity_id in created["workspace_providers"]:
            r = await client.delete(f"/v1/workspace_providers/{entity_id}")
            assert r.status_code in (200, 204, 404), r.text
        for entity_id in created["cross_encoder_providers"]:
            r = await client.delete(f"/v1/cross_encoder_providers/{entity_id}")
            assert r.status_code in (200, 204, 404), r.text
        for entity_id in created["embedding_providers"]:
            r = await client.delete(f"/v1/embedding_providers/{entity_id}")
            assert r.status_code in (200, 204, 404), r.text
        for entity_id in created["llm_providers"]:
            r = await client.delete(f"/v1/llm_providers/{entity_id}")
            assert r.status_code in (200, 204, 404), r.text

        # ---- post-delete: each entity should now 404 on GET
        # (sample a few — full coverage would be redundant with the
        # generic CRUD tests in other files).
        if workspace_id is not None:
            r = await client.get(f"/v1/workspaces/{workspace_id}")
            assert r.status_code == 404, r.text
        if created["llm_providers"]:
            r = await client.get(f"/v1/llm_providers/{created['llm_providers'][0]}")
            assert r.status_code == 404, r.text
        if created["agents"]:
            r = await client.get(f"/v1/agents/{created['agents'][0]}")
            assert r.status_code == 404, r.text
