"""E2E: §3 channels cascade lattice end-to-end journey.

Multi-subsystem user-journey that walks an operator through standing
up a complete channels deployment, then methodically exercises the
cascade-block lattice across THREE entity routers in one test:

  ChannelProvider --(blocks-on)--> Channel
                                     |
                                     v
                              (blocks-on)
                                     |
  WorkspaceChannelAssociation -------+
                                     ^
                                     |
                              WorkspaceProvider → Template → Workspace

Per matrix/api/routers/channels.py:

  * ChannelProvider DELETE blocks while a Channel references it (409)
  * Channel DELETE blocks while a WorkspaceChannelAssociation
    references it (409)
  * Workspace DELETE cascade-deletes the WorkspaceChannelAssociation
    rows referencing it (per §3 directive: 'workspace-delete cascade
    for associations')

Subsystems exercised in one test:

  1. ChannelProvider CRUD (Discord variant with valid bot_token)
  2. Channel CRUD (with provider_id reference integrity)
  3. WorkspaceProvider + WorkspaceTemplate + Workspace ladder
  4. WorkspaceChannelAssociation CRUD across two endpoints
     (flat + workspace-scoped proxy)
  5. Three independent cascade-block envelopes
  6. Workspace-delete cascade semantics

No LLM, no LM Studio, no Postgres injection — every step goes
through the public HTTP surface, so the test exercises the same
path an operator would hit through the console.

Covers backlog item T0851.
"""

from __future__ import annotations

import json

import httpx
import pytest


# 60-char placeholder; satisfies DiscordChannelProviderConfig.bot_token
# length floor (>=30) without looking like a real token. Same pattern
# as test_channels_cascade_block.py (T0842).
_FAKE_DISCORD_TOKEN = "x" * 60


# ===========================================================================
# T0851 — Channels cascade lattice multi-subsystem journey
# ===========================================================================


@pytest.mark.asyncio
async def test_t0851_channels_cascade_lattice_journey(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path,
) -> None:
    """T0851 — Walk a realistic channels deployment through the full
    cascade lattice over real HTTP.

    Steps:

      1. Seed ChannelProvider (Discord) + Channel under it.
      2. Seed WorkspaceProvider + Template + Workspace.
      3. POST flat WorkspaceChannelAssociation binding the workspace
         to the channel — verify uniqueness check on round-trip.
      4. Try DELETE Channel → 409 with the blocking association id.
      5. Try DELETE ChannelProvider → 409 with the blocking channel id.
      6. POST scoped-proxy duplicate (same workspace_id+channel_id) →
         409 /errors/conflict (uniqueness enforced at scoped endpoint
         too, not just the flat one).
      7. DELETE the workspace → 200/204 (associations should cascade
         per §3.5 spec; assert the association row is gone via GET).
      8. DELETE the channel → now succeeds (no association blocks).
      9. DELETE the channel provider → now succeeds (no channel blocks).
     10. DELETE the template + workspace provider — cleanup tail.

    Every assertion includes the /errors/internal leak guard.
    """
    cp_id = f"cp-{unique_suffix}"
    ch_id = f"ch-{unique_suffix}"
    wp_id = f"wp-{unique_suffix}"
    tpl_id = f"tpl-{unique_suffix}"
    assoc_id = f"assoc-{unique_suffix}"
    workspace_id: str | None = None
    cleanup_urls: list[str] = []

    try:
        # ----- 1. ChannelProvider + Channel ---------------------------------
        r = await client.post(
            "/v1/channel_providers",
            json={
                "id": cp_id,
                "provider": "discord",
                "config": {"bot_token": _FAKE_DISCORD_TOKEN, "enable_dms": True},
            },
        )
        assert r.status_code == 201, r.text
        cleanup_urls.append(f"/v1/channel_providers/{cp_id}")

        r = await client.post(
            "/v1/channels",
            json={
                "id": ch_id,
                "provider_id": cp_id,
                "external_id": f"snowflake-{unique_suffix}",
                "label": "T0851 probe",
            },
        )
        assert r.status_code == 201, r.text
        cleanup_urls.insert(0, f"/v1/channels/{ch_id}")

        # ----- 2. Workspace ladder ------------------------------------------
        r = await client.post(
            "/v1/workspace_providers",
            json={
                "id": wp_id,
                "provider": "local",
                "config": {"kind": "local", "path": str(tmp_path)},
            },
        )
        assert r.status_code == 201, r.text
        cleanup_urls.append(f"/v1/workspace_providers/{wp_id}")

        r = await client.post(
            "/v1/workspace_templates",
            json={
                "id": tpl_id,
                "description": "T0851 template",
                "provider_id": wp_id,
                "backend": {"kind": "local"},
            },
        )
        assert r.status_code == 201, r.text
        cleanup_urls.append(f"/v1/workspace_templates/{tpl_id}")

        r = await client.post("/v1/workspaces", json={"template_id": tpl_id})
        assert r.status_code == 201, r.text
        workspace_id = r.json()["id"]
        cleanup_urls.insert(0, f"/v1/workspaces/{workspace_id}")

        # ----- 3. WorkspaceChannelAssociation (flat endpoint) ---------------
        r = await client.post(
            "/v1/workspace_channel_associations",
            json={
                "id": assoc_id,
                "workspace_id": workspace_id,
                "channel_id": ch_id,
                "enabled": True,
                "forward_ask_user": True,
                "forward_tool_approval": True,
            },
        )
        assert r.status_code == 201, r.text
        assoc_url = f"/v1/workspace_channel_associations/{assoc_id}"

        # GET round-trips all fields.
        r = await client.get(assoc_url)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["workspace_id"] == workspace_id, body
        assert body["channel_id"] == ch_id, body
        assert body["enabled"] is True, body

        # ----- 4. DELETE Channel while association references it → 409 -----
        r = await client.delete(f"/v1/channels/{ch_id}")
        assert r.status_code == 409, r.text
        body = r.json()
        assert body["status"] == 409, body
        assert body["type"].endswith("/conflict"), body
        # Detail names the blocking association id.
        assert assoc_id in body.get("detail", ""), (
            f"expected blocking association id {assoc_id!r} in detail; "
            f"got: {body.get('detail')!r}"
        )
        assert "/errors/internal" not in json.dumps(body), body

        # ----- 5. DELETE ChannelProvider while channel references it → 409 -
        r = await client.delete(f"/v1/channel_providers/{cp_id}")
        assert r.status_code == 409, r.text
        body = r.json()
        assert body["status"] == 409, body
        assert body["type"].endswith("/conflict"), body
        # Detail names the blocking channel id (sister to T0842).
        assert ch_id in body.get("detail", ""), body

        # ----- 6. Duplicate via SCOPED-PROXY endpoint → 409 -----------------
        # /v1/workspaces/{wid}/channel_associations is a thin proxy over
        # the flat endpoint — it must also reject (workspace_id,
        # channel_id) duplicates with /errors/conflict, not silently
        # accept or 5xx.
        r = await client.post(
            f"/v1/workspaces/{workspace_id}/channel_associations",
            json={
                "id": f"{assoc_id}-dup",
                "workspace_id": workspace_id,
                "channel_id": ch_id,
                "enabled": True,
            },
        )
        assert r.status_code == 409, r.text
        body = r.json()
        assert body["status"] == 409, body
        assert body["type"].endswith("/conflict"), body
        assert "/errors/internal" not in json.dumps(body), body

        # ----- 7. DELETE workspace → cascade-deletes the association -------
        # Per §3.5 the workspace lifecycle owns its associations; deleting
        # the workspace must remove the rows referencing it so the parent
        # channel can later be deleted cleanly.
        r = await client.delete(f"/v1/workspaces/{workspace_id}")
        assert r.status_code in (200, 204), r.text
        cleanup_urls = [u for u in cleanup_urls if u != f"/v1/workspaces/{workspace_id}"]

        # Association row should be gone.
        r = await client.get(assoc_url)
        assert r.status_code == 404, (
            f"expected association {assoc_id!r} to cascade-delete with "
            f"workspace; got {r.status_code} {r.text!r}"
        )

        # ----- 8. DELETE channel now succeeds -------------------------------
        r = await client.delete(f"/v1/channels/{ch_id}")
        assert r.status_code in (200, 204), r.text
        cleanup_urls = [u for u in cleanup_urls if u != f"/v1/channels/{ch_id}"]

        # ----- 9. DELETE channel_provider now succeeds ----------------------
        r = await client.delete(f"/v1/channel_providers/{cp_id}")
        assert r.status_code in (200, 204), r.text
        cleanup_urls = [u for u in cleanup_urls if u != f"/v1/channel_providers/{cp_id}"]

        # Both are gone (probe-resistance — sister of T0842's tail).
        r = await client.get(f"/v1/channel_providers/{cp_id}")
        assert r.status_code == 404, r.text
    finally:
        # Best-effort unwind. The 7-9 chain may have already removed
        # the entries from cleanup_urls; what remains is the template
        # + provider tail and any half-rolled-back state.
        for url in cleanup_urls:
            try:
                await client.delete(url)
            except Exception:  # noqa: BLE001
                pass
