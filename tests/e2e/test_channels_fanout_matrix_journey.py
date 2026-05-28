"""E2E: multi-channel × multi-workspace association fan-out primer journey.

Multi-subsystem journey that stands up a 2×2 association primer
(2 ChannelProviders → 2 Channels → 2 Workspaces → 4 cross-product
WorkspaceChannelAssociations), then exercises the lattice end-to-end:

  1. Per-platform discriminated provider configs (Discord + Telegram).
  2. Channel CRUD with provider-reference integrity.
  3. WorkspaceChannelAssociation cross-product CRUD (4 rows).
  4. POST /workspace_channel_associations/find — predicate engine
     filtered by `workspace_id`; verify the filter returns exactly
     the 2 associations for that workspace.
  5. PUT one association — flip `enabled=false`; verify via GET.
  6. PUT another — flip `forward_ask_user=false` on a different
     association; verify isolation (didn't touch any other row).
  7. DELETE one Workspace → its 2 associations cascade-delete (per
     T0851 contract); the other Workspace's 2 associations survive.
  8. DELETE remaining infra in reverse cascade order; assert each
     step's HTTP envelope (204/200 path).

Subsystems exercised in one test:

  * ChannelProvider + Channel + WorkspaceChannelAssociation CRUD
    (3 routers)
  * WorkspaceProvider + Template + Workspace ladder (3 routers)
  * Per-platform discriminated config (Discord ≥30-char bot_token +
    enable_dms; Telegram ``<id>:<hash>`` bot_token shape)
  * find-with-predicate engine via /workspace_channel_associations/find
  * PUT-replace semantics on association toggles (the API requires
    the FULL row in the PUT body — assert we build it correctly from
    the GET response)
  * Cross-row isolation: flipping one association's toggle must not
    affect any other association's state
  * Workspace-delete cascade: the 2 associations referencing the
    deleted workspace are removed transactionally; the other 2
    survive

Covers backlog item T0855. No LLM, no real network calls — pure
HTTP CRUD + predicate-engine + cascade-engine exercise.
"""

from __future__ import annotations

import json

import httpx
import pytest


# 60-char placeholder for Discord; 30-char ``123456:abc…`` placeholder
# for Telegram. Each satisfies its provider's validator (Discord ≥30
# chars; Telegram ``<id>:<hash>`` shape ≥20 chars).
_FAKE_DISCORD_TOKEN = "x" * 60
_FAKE_TELEGRAM_TOKEN = "123456:" + "a" * 30


# ===========================================================================
# T0855 — Multi-channel × multi-workspace fan-out primer journey
# ===========================================================================


@pytest.mark.asyncio
async def test_t0855_multi_channel_multi_workspace_fanout_matrix(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path,
) -> None:
    """T0855 — Walk the full 2×2 cross-product association lattice.

    Steps:

      1. Seed 2 ChannelProviders (Discord + Telegram) with valid
         discriminated configs.
      2. Seed 2 Channels — one under each provider.
      3. Seed 2 Workspaces via the usual provider→template→workspace
         ladder.
      4. Create 4 WorkspaceChannelAssociations (cross-product).
      5. find-with-predicate: assert the workspace_id=W1 predicate
         returns exactly the 2 associations for W1.
      6. PUT one association → flip enabled=false; verify via GET.
      7. PUT another → flip forward_ask_user=false; verify isolation
         (the other 3 untouched).
      8. DELETE workspace W1 → the 2 associations referencing W1
         cascade-delete; the 2 referencing W2 survive untouched.
      9. Reverse-order cleanup tail.
    """
    suffix = unique_suffix
    # ChannelProviders
    cp_discord_id = f"cp-disc-{suffix}"
    cp_telegram_id = f"cp-tg-{suffix}"
    # Channels
    ch_discord_id = f"ch-disc-{suffix}"
    ch_telegram_id = f"ch-tg-{suffix}"
    # Workspace ladder × 2
    wp_id = f"wp-{suffix}"
    tpl_id = f"tpl-{suffix}"
    workspace_a_id: str | None = None
    workspace_b_id: str | None = None
    # Associations (cross-product: 4 rows)
    assoc_a_disc_id = f"assoc-a-disc-{suffix}"
    assoc_a_tg_id = f"assoc-a-tg-{suffix}"
    assoc_b_disc_id = f"assoc-b-disc-{suffix}"
    assoc_b_tg_id = f"assoc-b-tg-{suffix}"

    cleanup_urls: list[str] = []
    try:
        # ----- 1. Seed 2 ChannelProviders ---------------------------------
        r = await client.post(
            "/v1/channel_providers",
            json={
                "id": cp_discord_id,
                "provider": "discord",
                "config": {
                    "bot_token": _FAKE_DISCORD_TOKEN,
                    "enable_dms": True,
                },
            },
        )
        assert r.status_code == 201, r.text
        cleanup_urls.append(f"/v1/channel_providers/{cp_discord_id}")

        r = await client.post(
            "/v1/channel_providers",
            json={
                "id": cp_telegram_id,
                "provider": "telegram",
                "config": {
                    "bot_token": _FAKE_TELEGRAM_TOKEN,
                    "poll_timeout_seconds": 25,
                },
            },
        )
        assert r.status_code == 201, r.text
        cleanup_urls.append(f"/v1/channel_providers/{cp_telegram_id}")

        # ----- 2. Seed 2 Channels (one per provider) ----------------------
        r = await client.post(
            "/v1/channels",
            json={
                "id": ch_discord_id,
                "provider_id": cp_discord_id,
                "external_id": f"snowflake-d-{suffix}",
                "label": "T0855 Discord ops",
            },
        )
        assert r.status_code == 201, r.text
        cleanup_urls.insert(0, f"/v1/channels/{ch_discord_id}")

        r = await client.post(
            "/v1/channels",
            json={
                "id": ch_telegram_id,
                "provider_id": cp_telegram_id,
                "external_id": f"chat-t-{suffix}",
                "label": "T0855 Telegram ops",
            },
        )
        assert r.status_code == 201, r.text
        cleanup_urls.insert(0, f"/v1/channels/{ch_telegram_id}")

        # ----- 3. Seed 2 Workspaces via shared ladder ---------------------
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
                "description": "T0855 fan-out template",
                "provider_id": wp_id,
                "backend": {"kind": "local"},
            },
        )
        assert r.status_code == 201, r.text
        cleanup_urls.append(f"/v1/workspace_templates/{tpl_id}")

        r = await client.post("/v1/workspaces", json={"template_id": tpl_id})
        assert r.status_code == 201, r.text
        workspace_a_id = r.json()["id"]
        r = await client.post("/v1/workspaces", json={"template_id": tpl_id})
        assert r.status_code == 201, r.text
        workspace_b_id = r.json()["id"]

        # ----- 4. Cross-product associations (4 rows) ---------------------
        # Workspace A × {Discord, Telegram}
        r = await client.post("/v1/workspace_channel_associations", json={
            "id": assoc_a_disc_id,
            "workspace_id": workspace_a_id,
            "channel_id": ch_discord_id,
            "enabled": True,
            "forward_ask_user": True,
            "forward_tool_approval": True,
        })
        assert r.status_code == 201, r.text
        r = await client.post("/v1/workspace_channel_associations", json={
            "id": assoc_a_tg_id,
            "workspace_id": workspace_a_id,
            "channel_id": ch_telegram_id,
            "enabled": True,
            "forward_ask_user": True,
            "forward_tool_approval": True,
        })
        assert r.status_code == 201, r.text

        # Workspace B × {Discord, Telegram}
        r = await client.post("/v1/workspace_channel_associations", json={
            "id": assoc_b_disc_id,
            "workspace_id": workspace_b_id,
            "channel_id": ch_discord_id,
            "enabled": True,
            "forward_ask_user": True,
            "forward_tool_approval": True,
        })
        assert r.status_code == 201, r.text
        r = await client.post("/v1/workspace_channel_associations", json={
            "id": assoc_b_tg_id,
            "workspace_id": workspace_b_id,
            "channel_id": ch_telegram_id,
            "enabled": True,
            "forward_ask_user": True,
            "forward_tool_approval": True,
        })
        assert r.status_code == 201, r.text

        # ----- 5. find-with-predicate filtered by workspace_id -----------
        r = await client.post(
            "/v1/workspace_channel_associations/find",
            json={
                "predicate": {
                    "kind": "predicate",
                    "left": {"kind": "field", "name": "workspace_id"},
                    "op": "=",
                    "right": {"kind": "value", "value": workspace_a_id},
                },
                "page": {"kind": "offset", "offset": 0, "length": 100},
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        items = body["items"]
        assert len(items) == 2, (
            f"expected exactly 2 rows for workspace_a; got {len(items)}: "
            f"{[i['id'] for i in items]!r}"
        )
        returned_ids = {i["id"] for i in items}
        assert returned_ids == {assoc_a_disc_id, assoc_a_tg_id}, (
            f"unexpected ids in predicate result: {returned_ids!r}"
        )
        # No /errors/internal leaked.
        assert "/errors/internal" not in json.dumps(body), body

        # ----- 6. PUT to flip enabled=false on assoc_a_disc -------------
        # Get the row to see its current shape (PUT replaces whole row).
        r = await client.get(
            f"/v1/workspace_channel_associations/{assoc_a_disc_id}",
        )
        assert r.status_code == 200, r.text
        row = r.json()
        row["enabled"] = False
        r = await client.put(
            f"/v1/workspace_channel_associations/{assoc_a_disc_id}",
            json=row,
        )
        assert r.status_code == 200, r.text

        r = await client.get(
            f"/v1/workspace_channel_associations/{assoc_a_disc_id}",
        )
        assert r.status_code == 200, r.text
        assert r.json()["enabled"] is False, r.text

        # ----- 7. PUT to flip forward_ask_user=false on assoc_b_tg ------
        r = await client.get(
            f"/v1/workspace_channel_associations/{assoc_b_tg_id}",
        )
        assert r.status_code == 200, r.text
        row = r.json()
        row["forward_ask_user"] = False
        r = await client.put(
            f"/v1/workspace_channel_associations/{assoc_b_tg_id}",
            json=row,
        )
        assert r.status_code == 200, r.text

        # Verify cross-row isolation: the OTHER 3 rows still have their
        # original flags. The two we flipped are the only ones touched.
        for aid, expected_enabled, expected_forward_ask in (
            (assoc_a_disc_id, False, True),     # step 6 touched
            (assoc_a_tg_id, True, True),         # untouched
            (assoc_b_disc_id, True, True),       # untouched
            (assoc_b_tg_id, True, False),        # step 7 touched
        ):
            r = await client.get(
                f"/v1/workspace_channel_associations/{aid}",
            )
            assert r.status_code == 200, r.text
            row = r.json()
            assert row["enabled"] == expected_enabled, (
                f"row {aid}: expected enabled={expected_enabled}, "
                f"got {row['enabled']}"
            )
            assert row["forward_ask_user"] == expected_forward_ask, (
                f"row {aid}: expected forward_ask_user={expected_forward_ask}, "
                f"got {row['forward_ask_user']}"
            )

        # ----- 8. DELETE workspace A → its 2 associations cascade-delete -
        r = await client.delete(f"/v1/workspaces/{workspace_a_id}")
        assert r.status_code in (200, 204), r.text

        # A's 2 associations are gone.
        r = await client.get(
            f"/v1/workspace_channel_associations/{assoc_a_disc_id}",
        )
        assert r.status_code == 404, (
            f"expected workspace-delete to cascade {assoc_a_disc_id}; "
            f"got {r.status_code} {r.text!r}"
        )
        r = await client.get(
            f"/v1/workspace_channel_associations/{assoc_a_tg_id}",
        )
        assert r.status_code == 404, (
            f"expected workspace-delete to cascade {assoc_a_tg_id}; "
            f"got {r.status_code} {r.text!r}"
        )

        # B's 2 associations survive untouched — proves the cascade only
        # affected rows referencing the deleted workspace.
        r = await client.get(
            f"/v1/workspace_channel_associations/{assoc_b_disc_id}",
        )
        assert r.status_code == 200, r.text
        r = await client.get(
            f"/v1/workspace_channel_associations/{assoc_b_tg_id}",
        )
        assert r.status_code == 200, r.text

        # ----- 9. Cleanup tail (reverse cascade order) -------------------
        # B's associations first, then workspace B, then template +
        # provider, then channels, then channel providers.
        r = await client.delete(
            f"/v1/workspace_channel_associations/{assoc_b_disc_id}",
        )
        assert r.status_code in (200, 204), r.text
        r = await client.delete(
            f"/v1/workspace_channel_associations/{assoc_b_tg_id}",
        )
        assert r.status_code in (200, 204), r.text
        r = await client.delete(f"/v1/workspaces/{workspace_b_id}")
        assert r.status_code in (200, 204), r.text

        # Channels + ChannelProviders (now unblocked).
        r = await client.delete(f"/v1/channels/{ch_discord_id}")
        assert r.status_code in (200, 204), r.text
        cleanup_urls = [u for u in cleanup_urls
                        if u != f"/v1/channels/{ch_discord_id}"]
        r = await client.delete(f"/v1/channels/{ch_telegram_id}")
        assert r.status_code in (200, 204), r.text
        cleanup_urls = [u for u in cleanup_urls
                        if u != f"/v1/channels/{ch_telegram_id}"]
        r = await client.delete(f"/v1/channel_providers/{cp_discord_id}")
        assert r.status_code in (200, 204), r.text
        cleanup_urls = [u for u in cleanup_urls
                        if u != f"/v1/channel_providers/{cp_discord_id}"]
        r = await client.delete(f"/v1/channel_providers/{cp_telegram_id}")
        assert r.status_code in (200, 204), r.text
        cleanup_urls = [u for u in cleanup_urls
                        if u != f"/v1/channel_providers/{cp_telegram_id}"]
    finally:
        # Best-effort safety net. The happy-path teardown above drained
        # most entries; this catches anything left if the test bailed
        # mid-way.
        for url in reversed(cleanup_urls):
            try:
                await client.delete(url)
            except Exception:  # noqa: BLE001
                pass
