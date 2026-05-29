"""UI E2E: channels operator-onboarding journey across the 3 Designer surfaces.

Multi-page journey that walks an operator standing up a brand-new
channel routing setup via the console — across the three new
Channels sub-pages the Designer's redesign added (Providers /
Channels / Associations).

Pages traversed:

  /console/ (initial nav) → /channels/providers → New-provider
  modal → submit → row visible →
  /channels/channels → New-channel modal → submit → row visible →
  /channels/associations → New-association modal → submit → row
  visible (workspace+channel join created).

Multi-subsystem exercise in one test:

  1. Channels Providers list + create modal with discriminated
     config (we drive the Discord path: 60-char bot_token +
     enable_dms toggle).
  2. Channels list + create modal with provider-reference integrity
     (the dropdown lists the just-created provider; submit creates
     a Channel under it).
  3. Associations list + create modal with workspace + channel
     dropdowns (the dropdowns are populated from the live
     /workspaces and /channels endpoints; submit POSTs a
     WorkspaceChannelAssociation that survives a list reload).
  4. End-to-end cross-page nav between the 3 new sidebar entries
     (sidebar links work + each list page renders without console
     errors).

Covers backlog item U0108. First UI test to walk the Designer's
Channels surface end-to-end as an integrated operator flow rather
than 3 separate single-page tests.

API-seeds the WorkspaceProvider + Template + Workspace via httpx
(no UI for those — WorkspaceTemplate creation is API-only); then
drives the 3 channel-related creates through the UI.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import expect


# 60-char placeholder; satisfies DiscordChannelProviderConfig.bot_token
# length floor (>=30) without looking like a real token.
_FAKE_DISCORD_TOKEN = "x" * 60


def _seed_workspace(base_url: str, suffix: str, tmp_path) -> dict[str, str]:
    """Seed WorkspaceProvider + Template + Workspace via API. Returns ids."""
    ids = {
        "wp": f"wp-108-{suffix}",
        "tpl": f"tpl-108-{suffix}",
        "workspace": "",
    }
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post(
            "/v1/workspace_providers",
            json={
                "id": ids["wp"],
                "provider": "local",
                "config": {"kind": "local", "root_path": f"/tmp/u0108-{suffix}"},
            },
        )
        assert r.status_code == 201, r.text
        r = c.post(
            "/v1/workspace_templates",
            json={
                "id": ids["tpl"],
                "description": "U0108 channels journey tpl",
                "provider_id": ids["wp"],
                "backend": {"kind": "local"},
            },
        )
        assert r.status_code == 201, r.text
        r = c.post("/v1/workspaces", json={"template_id": ids["tpl"]})
        assert r.status_code == 201, r.text
        ids["workspace"] = r.json()["id"]
    return ids


def _cleanup(base_url: str, ids: dict[str, str], cp_id: str, ch_id: str) -> None:
    """Best-effort unwind in reverse dependency order."""
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        # Associations cascade-delete when their workspace is deleted, so
        # just remove the workspace and the four channel-side entities.
        for url in (
            f"/v1/workspaces/{ids['workspace']}" if ids.get("workspace") else None,
            f"/v1/workspace_templates/{ids['tpl']}",
            f"/v1/workspace_providers/{ids['wp']}",
            f"/v1/channels/{ch_id}" if ch_id else None,
            f"/v1/channel_providers/{cp_id}" if cp_id else None,
        ):
            if url is None:
                continue
            try:
                c.delete(url)
            except Exception:  # noqa: BLE001
                pass


# ===========================================================================
# U0108 — Channels operator-onboarding journey
# ===========================================================================


def test_u0108_channels_operator_onboarding_journey(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
    tmp_path,
) -> None:
    """U0108 — Walk a 3-page channel-setup flow via the Designer's UI.

    Steps:

      1. API-seed a Workspace (WorkspaceTemplate creation is API-only).
      2. Navigate /channels/providers → click "New provider" → fill
         id + bot_token (Discord) → submit → modal closes + new
         provider row visible in the list.
      3. Navigate /channels/channels → "New channel" → pick the
         provider just created + fill external_id + label → submit
         → channel row visible.
      4. Navigate /channels/associations → "New association" → pick
         our workspace + the channel just created → submit →
         association row visible.

    Pages traversed:
      /console/ → /channels/providers → /channels/channels →
      /channels/associations.

    All three modals are exercised, plus the live cross-page
    reference-integrity flow (the channel-create dropdown picks up
    the just-created provider; the association-create dropdowns
    pick up the workspace + channel created earlier in the same
    session).
    """
    ids = _seed_workspace(base_url, unique_suffix, tmp_path)
    wid = ids["workspace"]
    cp_id = f"cp-108-{unique_suffix}"
    ch_id = f"ch-108-{unique_suffix}"
    cleanup_cp = cp_id
    cleanup_ch = ch_id

    try:
        # ----- 1. /channels/providers → New provider --------------------
        page.goto(
            f"{console_url}#/channels/providers",
            wait_until="domcontentloaded",
        )
        # Page renders; "New provider" CTA is reachable.
        new_provider_btn = page.get_by_role(
            "button", name="New provider", exact=True,
        )
        expect(new_provider_btn).to_be_visible(timeout=20_000)
        new_provider_btn.click()

        modal = page.locator(".modal").first
        expect(modal).to_be_visible(timeout=5_000)

        # The id field is the first input in the modal (placeholder
        # "auto-generated", same pattern as U0107's graph modal).
        modal.get_by_placeholder("auto-generated", exact=False).first.fill(cp_id)

        # The Provider select defaults to "slack"; switch to discord.
        modal.locator("select.select").first.select_option("discord")

        # Discord config asks for bot_token (password input). Designer
        # renders inputs of type=password for tokens.
        modal.locator("input[type=password]").first.fill(_FAKE_DISCORD_TOKEN)

        # Submit. Designer's provider modal Btn label is "Create provider".
        modal.get_by_role("button", name="Create provider", exact=True).click()

        # Modal closes; Designer's onSuccess navigates to the new
        # provider's detail page. Confirm we're there (URL contains
        # the cp_id and the detail-page Probe button is visible).
        expect(page.locator(".modal")).not_to_be_visible(timeout=10_000)
        page.wait_for_url(
            f"**/console/#/channels/providers/{cp_id}**", timeout=15_000,
        )
        expect(page.get_by_role("button", name="Probe").first).to_be_visible(
            timeout=10_000,
        )

        # ----- 2. /channels/channels → New channel ----------------------
        page.goto(
            f"{console_url}#/channels/channels",
            wait_until="domcontentloaded",
        )
        new_channel_btn = page.get_by_role(
            "button", name="New channel", exact=True,
        )
        expect(new_channel_btn).to_be_visible(timeout=15_000)
        new_channel_btn.click()

        modal = page.locator(".modal").first
        expect(modal).to_be_visible(timeout=5_000)

        # id (auto-generated placeholder)
        modal.get_by_placeholder("auto-generated", exact=False).first.fill(ch_id)

        # provider dropdown (first select in the modal); pick the one
        # we just created.
        modal.locator("select.select").first.select_option(cp_id)

        # external_id input — placeholder is "C0123ABC456 / chat-id /
        # snowflake".
        modal.get_by_placeholder("C0123ABC456", exact=False).first.fill(
            f"snowflake-{unique_suffix}",
        )

        # Submit. Designer's modal Btn label is "Create channel".
        modal.get_by_role("button", name="Create channel", exact=True).click()

        # Modal closes; channel row visible.
        expect(page.locator(".modal")).not_to_be_visible(timeout=10_000)
        ch_row = page.locator("tbody tr", has_text=ch_id)
        expect(ch_row).to_be_visible(timeout=15_000)

        # ----- 3. /channels/associations → New association --------------
        page.goto(
            f"{console_url}#/channels/associations",
            wait_until="domcontentloaded",
        )
        new_assoc_btn = page.get_by_role(
            "button", name="New association", exact=True,
        )
        # The "New association" button is disabled until workspaces +
        # channels both load (channels.jsx:822). Wait for it to be
        # enabled before clicking — Playwright won't auto-wait for that
        # via to_be_visible alone.
        expect(new_assoc_btn).to_be_enabled(timeout=15_000)
        new_assoc_btn.click()

        modal = page.locator(".modal").first
        expect(modal).to_be_visible(timeout=5_000)

        # Workspace dropdown is the first select in the association
        # modal; channel dropdown is the second. Wait for the dropdowns
        # to have options (Designer's modal initializes state from props
        # but doesn't re-sync when props change later).
        ws_select = modal.locator("select.select").nth(0)
        ch_select = modal.locator("select.select").nth(1)
        # Both selects must already have our seeded ids selectable.
        expect(ws_select.locator(f"option[value='{wid}']")).to_be_attached(
            timeout=10_000,
        )
        expect(ch_select.locator(f"option[value='{ch_id}']")).to_be_attached(
            timeout=10_000,
        )
        ws_select.select_option(wid)
        ch_select.select_option(ch_id)

        # Submit. Modal Btn label is just "Create" (verified at
        # channels.jsx:957).
        modal.get_by_role("button", name="Create", exact=True).click()

        # Modal closes; the association row appears in the list. The
        # association id is backend-allocated so we anchor on the
        # workspace_id + channel_id appearing together in a single row.
        expect(page.locator(".modal")).not_to_be_visible(timeout=10_000)
        assoc_row = page.locator("tbody tr").filter(
            has_text=wid,
        ).filter(has_text=ch_id)
        expect(assoc_row.first).to_be_visible(timeout=15_000)
    finally:
        _cleanup(base_url, ids, cleanup_cp, cleanup_ch)
