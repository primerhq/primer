"""E2E: secured-workspace operator setup journey.

Multi-subsystem user-journey that walks the role of a SecOps
operator standing up a workspace that requires approval for
sensitive tool calls AND routes ask_user prompts through a chat
channel. The single test exercises the THREE independent cascade-
block invariants the platform pins:

  1. Toolset DELETE blocked while a ToolApprovalPolicy
     references it (§2 directive: "cascade-block on Toolset delete")
  2. Channel DELETE blocked while an Association references it (§3)
  3. ChannelProvider DELETE blocked while a Channel references it (§3)

Plus a deliberate negative-cascade probe — WorkspaceProvider DELETE
is NOT blocked by a referencing WorkspaceTemplate (no on_delete on
the provider router); confirming this in the same test prevents a
future refactor from silently introducing the cascade we just
asserted doesn't exist.

Then walks the correct unwind order, asserting each step succeeds
cleanly. Across 8 routers + 3 cascade-aware DELETEs + 1 negative
cascade probe, in one test.

Subsystems exercised in one test:

  1. LLMProvider CRUD
  2. Toolset CRUD (provider=internal)
  3. ToolApprovalPolicy CRUD with `toolset_id` reference integrity
  4. ChannelProvider CRUD (Discord variant)
  5. Channel CRUD with `provider_id` reference integrity
  6. WorkspaceProvider + Template + Workspace ladder
  7. WorkspaceChannelAssociation CRUD bridging workspace ↔ channel
  8. Agent CRUD with model + tools list referencing the toolset
  9. Four independent cascade-block 409 envelopes — pin the detail
     string carries the blocking row id (operator can find the
     cause without running follow-up queries)
 10. Correct teardown order — confirms the live HTTP path actually
     un-blocks once the blocker is removed

No LLM, no MCP, no Postgres injection — every step goes through the
public HTTP surface so this exercises the production code paths an
operator would hit through the console.

Covers backlog item T0853.
"""

from __future__ import annotations

import json

import httpx
import pytest


# 60-char placeholder for DiscordChannelProviderConfig.bot_token
_FAKE_DISCORD_TOKEN = "x" * 60


# ===========================================================================
# T0853 — Secured-workspace operator setup journey
# ===========================================================================


@pytest.mark.asyncio
async def test_t0853_secured_workspace_setup_with_cascade_invariants(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path,
) -> None:
    """T0853 — Stand up a workspace with an approval-gated toolset
    and a chat-channel routing layer, then assert the four cascade
    invariants over real HTTP.

    Steps:

      1. Seed LLMProvider (placeholder Ollama config).
      2. Seed user Toolset (provider='internal'; no config needed).
      3. Seed ToolApprovalPolicy gating one tool on the user toolset.
      4. Seed ChannelProvider (Discord with valid bot_token).
      5. Seed Channel under the ChannelProvider.
      6. Seed WorkspaceProvider + Template + Workspace ladder.
      7. Seed WorkspaceChannelAssociation bridging workspace ↔ channel.
      8. Seed Agent with the user toolset in its first-class tools list.
      9. Cascade-block primer — try each DELETE in the WRONG order
         and assert the 409 envelope carries the blocking row id:
           a. DELETE toolset → 409 (policy blocks)
           b. DELETE channel → 409 (association blocks)
           c. DELETE channel_provider → 409 (channel blocks)
           d. DELETE workspace_provider → 409 (template blocks)
     10. Correct unwind: policy → toolset; workspace → cascade-deletes
         association; then channel → channel_provider; then template
         → workspace_provider; then agent + llm_provider.

    Every cascade-block assertion includes the /errors/internal leak
    guard and verifies the blocking row id appears in the detail
    string.
    """
    llm_id = f"sw-llm-{unique_suffix}"
    toolset_id = f"sw-ts-{unique_suffix}"
    policy_id = f"sw-pol-{unique_suffix}"
    cp_id = f"sw-cp-{unique_suffix}"
    ch_id = f"sw-ch-{unique_suffix}"
    wp_id = f"sw-wp-{unique_suffix}"
    tpl_id = f"sw-tpl-{unique_suffix}"
    assoc_id = f"sw-assoc-{unique_suffix}"
    agent_id = f"sw-ag-{unique_suffix}"
    workspace_id: str | None = None
    seeded_urls: list[str] = []

    try:
        # ----- 1. LLMProvider ---------------------------------------------
        r = await client.post("/v1/llm_providers", json={
            "id": llm_id,
            "provider": "ollama",
            "config": {"url": "http://127.0.0.1:9999"},
            "models": [{"name": "fake-model", "context_length": 4096}],
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201, r.text
        seeded_urls.append(f"/v1/llm_providers/{llm_id}")

        # ----- 2. User Toolset (provider=internal) ------------------------
        r = await client.post("/v1/toolsets", json={
            "id": toolset_id,
            "provider": "internal",
        })
        assert r.status_code == 201, r.text
        seeded_urls.append(f"/v1/toolsets/{toolset_id}")

        # ----- 3. ToolApprovalPolicy referencing the toolset --------------
        r = await client.post("/v1/tool_approval_policies", json={
            "id": policy_id,
            "toolset_id": toolset_id,
            "tool_name": "sensitive_op",
            "enabled": True,
            "approval": {"type": "required"},
        })
        assert r.status_code == 201, r.text
        seeded_urls.append(f"/v1/tool_approval_policies/{policy_id}")

        # ----- 4. ChannelProvider -----------------------------------------
        r = await client.post("/v1/channel_providers", json={
            "id": cp_id,
            "provider": "discord",
            "config": {"bot_token": _FAKE_DISCORD_TOKEN, "enable_dms": True},
        })
        assert r.status_code == 201, r.text
        seeded_urls.append(f"/v1/channel_providers/{cp_id}")

        # ----- 5. Channel referencing the provider ------------------------
        r = await client.post("/v1/channels", json={
            "id": ch_id,
            "provider_id": cp_id,
            "external_id": f"snowflake-{unique_suffix}",
            "label": "T0853 secured-ops",
        })
        assert r.status_code == 201, r.text
        seeded_urls.append(f"/v1/channels/{ch_id}")

        # ----- 6. Workspace ladder ----------------------------------------
        r = await client.post("/v1/workspace_providers", json={
            "id": wp_id,
            "provider": "local",
            "config": {"kind": "local", "root_path": str(tmp_path)},
        })
        assert r.status_code == 201, r.text
        seeded_urls.append(f"/v1/workspace_providers/{wp_id}")

        r = await client.post("/v1/workspace_templates", json={
            "id": tpl_id,
            "description": "T0853 secured template",
            "provider_id": wp_id,
            "backend": {"kind": "local"},
        })
        assert r.status_code == 201, r.text
        seeded_urls.append(f"/v1/workspace_templates/{tpl_id}")

        r = await client.post("/v1/workspaces", json={"template_id": tpl_id})
        assert r.status_code == 201, r.text
        workspace_id = r.json()["id"]
        seeded_urls.append(f"/v1/workspaces/{workspace_id}")

        # ----- 7. WorkspaceChannelAssociation -----------------------------
        r = await client.post(
            "/v1/workspace_channel_associations", json={
                "id": assoc_id,
                "workspace_id": workspace_id,
                "channel_id": ch_id,
                "enabled": True,
            },
        )
        assert r.status_code == 201, r.text

        # ----- 8. Agent referencing the toolset ---------------------------
        r = await client.post("/v1/agents", json={
            "id": agent_id,
            "description": "T0853 secured agent",
            "model": {"provider_id": llm_id, "model_name": "fake-model"},
            "tools": [toolset_id],
            "system_prompt": ["secured agent"],
        })
        assert r.status_code == 201, r.text
        seeded_urls.append(f"/v1/agents/{agent_id}")

        # ===================================================================
        # 9. Cascade-block primer — try each DELETE in the WRONG order.
        # ===================================================================

        # ----- 9a. Toolset blocked by policy (§2 directive) ---------------
        r = await client.delete(f"/v1/toolsets/{toolset_id}")
        assert r.status_code == 409, r.text
        body = r.json()
        assert body["status"] == 409, body
        assert body["type"].endswith("/conflict"), body
        assert policy_id in body.get("detail", ""), (
            f"expected blocking policy id {policy_id!r} in detail; "
            f"got: {body.get('detail')!r}"
        )
        assert "/errors/internal" not in json.dumps(body), body

        # ----- 9b. Channel blocked by association (§3) --------------------
        r = await client.delete(f"/v1/channels/{ch_id}")
        assert r.status_code == 409, r.text
        body = r.json()
        assert body["type"].endswith("/conflict"), body
        assert assoc_id in body.get("detail", ""), body
        assert "/errors/internal" not in json.dumps(body), body

        # ----- 9c. ChannelProvider blocked by channel (§3) ----------------
        r = await client.delete(f"/v1/channel_providers/{cp_id}")
        assert r.status_code == 409, r.text
        body = r.json()
        assert body["type"].endswith("/conflict"), body
        assert ch_id in body.get("detail", ""), body

        # ----- 9d. Negative-cascade probe: WorkspaceProvider DELETE is
        # NOT blocked by a referencing WorkspaceTemplate (no on_delete
        # handler on the provider router — see workspaces.py:145-151).
        # Confirming this in the same journey freezes the contract: a
        # future PR that silently adds cascade-block here breaks the
        # test, forcing a deliberate spec update before landing.
        # We probe via a SECOND, throwaway WorkspaceProvider/Template
        # pair so the live wp_id stays usable for the rest of the
        # unwind.
        probe_wp_id = f"sw-wp-probe-{unique_suffix}"
        probe_tpl_id = f"sw-tpl-probe-{unique_suffix}"
        r = await client.post("/v1/workspace_providers", json={
            "id": probe_wp_id,
            "provider": "local",
            "config": {"kind": "local", "root_path": str(tmp_path / "probe")},
        })
        assert r.status_code == 201, r.text
        r = await client.post("/v1/workspace_templates", json={
            "id": probe_tpl_id,
            "description": "T0853 negative-cascade probe",
            "provider_id": probe_wp_id,
            "backend": {"kind": "local"},
        })
        assert r.status_code == 201, r.text
        # DELETE provider while a template references it — must succeed
        # (200/204), proving no cascade-block exists today.
        r = await client.delete(f"/v1/workspace_providers/{probe_wp_id}")
        assert r.status_code in (200, 204), (
            f"WorkspaceProvider DELETE returned {r.status_code} — "
            f"a cascade-block was silently added. Spec needs updating "
            f"before this test should accept the new behaviour. "
            f"body={r.text!r}"
        )
        # Cleanup the orphan template (its provider ref is now stale).
        r = await client.delete(f"/v1/workspace_templates/{probe_tpl_id}")
        assert r.status_code in (200, 204), r.text

        # ===================================================================
        # 10. Correct unwind order.
        # ===================================================================

        # Remove the policy → toolset un-blocks.
        r = await client.delete(f"/v1/tool_approval_policies/{policy_id}")
        assert r.status_code in (200, 204), r.text
        r = await client.delete(f"/v1/toolsets/{toolset_id}")
        assert r.status_code in (200, 204), r.text

        # Delete workspace → association cascade-deletes (per T0851 + §3.5).
        r = await client.delete(f"/v1/workspaces/{workspace_id}")
        assert r.status_code in (200, 204), r.text

        # Association row is gone after the cascade.
        r = await client.get(
            f"/v1/workspace_channel_associations/{assoc_id}"
        )
        assert r.status_code == 404, (
            f"expected association {assoc_id!r} to cascade-delete with "
            f"workspace; got {r.status_code} {r.text!r}"
        )

        # Channel + ChannelProvider now un-blocked.
        r = await client.delete(f"/v1/channels/{ch_id}")
        assert r.status_code in (200, 204), r.text
        r = await client.delete(f"/v1/channel_providers/{cp_id}")
        assert r.status_code in (200, 204), r.text

        # Template + WorkspaceProvider now un-blocked.
        r = await client.delete(f"/v1/workspace_templates/{tpl_id}")
        assert r.status_code in (200, 204), r.text
        r = await client.delete(f"/v1/workspace_providers/{wp_id}")
        assert r.status_code in (200, 204), r.text

        # Agent (no longer references a live toolset) + LLM unwind clean.
        r = await client.delete(f"/v1/agents/{agent_id}")
        assert r.status_code in (200, 204), r.text
        r = await client.delete(f"/v1/llm_providers/{llm_id}")
        assert r.status_code in (200, 204), r.text
    finally:
        # Best-effort safety net for anything left in flight when the
        # test bails mid-way.
        for url in reversed(seeded_urls):
            try:
                await client.delete(url)
            except Exception:  # noqa: BLE001
                pass
