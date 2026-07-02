"""UI E2E: channels operator-onboarding journey.

Multi-page journey that walks an operator standing up a brand-new
channel routing setup via the console — from provider, to a
chat-enabled channel, to linking that channel onto a workspace.

The Designer's Channels surface is two sub-pages (Providers /
Channels); the per-workspace channel link now lives on the
WORKSPACE DETAIL page (the standalone "Associations" page + nav
item were removed when the channel link became a field on the
Workspace row).

Pages traversed:

  /console/ (initial nav) → /channels/providers → New-provider
  modal → submit → provider detail →
  /channels/channels → New-channel modal (with the "Chats"
  fieldset: enabled + default_agent) → submit → row visible →
  /workspaces/{wid}?tab=channels → "Link channel" modal → submit
  → channel shown as linked on the workspace.

Multi-subsystem exercise in one test:

  1. Channels Providers list + create modal with discriminated
     config (we drive the Discord path: 60-char bot_token).
  2. Channels list + create modal with provider-reference integrity
     (the dropdown lists the just-created provider) PLUS the Chats
     fieldset — chats enabled + a default_agent picked from the
     live /agents endpoint.
  3. Workspace-detail Channels tab → Link-channel modal: the
     channel dropdown is populated from the live /channels endpoint;
     submit PUTs /workspaces/{wid}/channel_association and the link
     survives a refetch.
  4. End-to-end cross-page nav between the channels sidebar entries
     and the workspace detail page (links work + each list page
     renders without console errors).

Covers backlog item U0108.

API-seeds an Agent (for the channel's default_agent) plus the
WorkspaceProvider + Template + Workspace via httpx (no UI for those
— WorkspaceTemplate creation is API-only); then drives the
provider + channel creates through the UI and links the channel to
the workspace on the workspace detail page.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import expect

from tests.ui_e2e._studio_helpers import open_workspace_settings


# 60-char placeholder; satisfies DiscordChannelProviderConfig.bot_token
# length floor (>=30) without looking like a real token.
_FAKE_DISCORD_TOKEN = "x" * 60


def _seed(base_url: str, suffix: str) -> dict[str, str]:
    """Seed Agent + WorkspaceProvider + Template + Workspace via API.

    Returns ids. The Agent is needed so the channel-create modal's
    "Chats" fieldset has a default_agent to select; the workspace is
    the link target on the workspace detail page.
    """
    ids = {
        "agent": f"ag-108-{suffix}",
        "llm": f"llm-108-{suffix}",
        "wp": f"wp-108-{suffix}",
        "tpl": f"tpl-108-{suffix}",
        "workspace": "",
    }
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post(
            "/v1/llm_providers",
            json={
                "id": ids["llm"],
                "provider": "ollama",
                "config": {"url": "http://127.0.0.1:9999"},
                "models": [{"name": "fake-model", "context_length": 4096}],
                "limits": {"max_concurrency": 1},
            },
        )
        assert r.status_code == 201, r.text
        r = c.post(
            "/v1/agents",
            json={
                "id": ids["agent"],
                "description": "U0108 channels journey default agent",
                "model": {
                    "provider_id": ids["llm"], "model_name": "fake-model",
                },
                "tools": [],
                "system_prompt": ["u0108"],
            },
        )
        assert r.status_code == 201, r.text
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
        # The workspace carries the channel link as a field, so deleting
        # the workspace drops it. Then remove the channel-side entities
        # and the agent/llm.
        for url in (
            f"/v1/workspaces/{ids['workspace']}" if ids.get("workspace") else None,
            f"/v1/workspace_templates/{ids['tpl']}",
            f"/v1/workspace_providers/{ids['wp']}",
            f"/v1/channels/{ch_id}" if ch_id else None,
            f"/v1/channel_providers/{cp_id}" if cp_id else None,
            f"/v1/agents/{ids['agent']}",
            f"/v1/llm_providers/{ids['llm']}",
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
) -> None:
    """U0108 — Walk a provider→channel→workspace-link setup flow via UI.

    Steps:

      1. API-seed an Agent + a Workspace (WorkspaceTemplate creation
         is API-only).
      2. Navigate /channels/providers → click "New provider" → fill
         id + bot_token (Discord) → submit → provider detail page.
      3. Navigate /channels/channels → "New channel" → pick the
         provider just created + fill external_id, enable Chats +
         pick the seeded agent as default_agent → submit → channel
         row visible.
      4. Navigate /workspaces/{wid}?tab=channels → "Link channel"
         modal → pick the channel just created → submit → the
         channel shows as linked on the workspace.

    Pages traversed:
      /console/ → /channels/providers → /channels/channels →
      /workspaces/{wid}?tab=channels.
    """
    ids = _seed(base_url, unique_suffix)
    wid = ids["workspace"]
    agent_id = ids["agent"]
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
        new_provider_btn = page.get_by_role(
            "button", name="New provider", exact=True,
        )
        expect(new_provider_btn).to_be_visible(timeout=20_000)
        new_provider_btn.click()

        modal = page.locator(".modal").first
        expect(modal).to_be_visible(timeout=5_000)

        # The id field is the first input in the modal (placeholder
        # "auto-generated").
        modal.get_by_placeholder("auto-generated", exact=False).first.fill(cp_id)

        # The platform select defaults to "slack"; switch to discord.
        modal.locator("select.select").first.select_option("discord")

        # Discord config asks for bot_token (password input).
        modal.locator("input[type=password]").first.fill(_FAKE_DISCORD_TOKEN)

        # Submit. Provider modal Btn label is "Create provider".
        modal.get_by_role("button", name="Create provider", exact=True).click()

        # Modal closes; Designer's onSuccess navigates to the new
        # provider's detail page.
        expect(page.locator(".modal")).not_to_be_visible(timeout=10_000)
        page.wait_for_url(
            f"**/console/#/channels/providers/{cp_id}**", timeout=15_000,
        )
        expect(page.get_by_role("button", name="Probe").first).to_be_visible(
            timeout=10_000,
        )

        # ----- 2. /channels/channels → New channel (with Chats) ---------
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
        # we just created. Option text is "{id} ({provider})".
        modal.locator("select.select").first.select_option(cp_id)

        # external_id input — placeholder is "C0123ABC456 / chat-id /
        # snowflake".
        modal.get_by_placeholder("C0123ABC456", exact=False).first.fill(
            f"snowflake-{unique_suffix}",
        )

        # ----- Chats fieldset: enable + pick the seeded default_agent.
        # "Chats enabled" is a switch-style toggle button; clicking it reveals
        # the rest of the chat controls (default_agent, relay mode, etc.).
        chats_enabled = modal.get_by_test_id("channel-chats-enabled")
        chats_enabled.click()
        expect(chats_enabled).to_have_attribute("aria-checked", "true")

        # The default_agent select is the second select.mono in the modal
        # (the first is the provider dropdown). Wait for the seeded agent
        # to be available as an option, then select it.
        default_agent_select = modal.locator("select.select").nth(1)
        expect(
            default_agent_select.locator(f"option[value='{agent_id}']")
        ).to_be_attached(timeout=10_000)
        default_agent_select.select_option(agent_id)

        # Submit. Channel modal Btn label is "Create channel".
        modal.get_by_role("button", name="Create channel", exact=True).click()

        # Modal closes; channel row visible in the list.
        expect(page.locator(".modal")).not_to_be_visible(timeout=10_000)
        ch_row = page.locator("tbody tr", has_text=ch_id)
        expect(ch_row).to_be_visible(timeout=15_000)

        # Verify the chats config landed via the API (the UI doesn't
        # surface it on the list row).
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            r = c.get(f"/v1/channels/{ch_id}")
            assert r.status_code == 200, r.text
            chats = r.json().get("config", {}).get("chats", {})
            assert chats.get("enabled") is True, r.json()
            assert chats.get("default_agent") == agent_id, r.json()

        # ----- 3. Studio → Settings modal → Channels → Link channel -----
        # Re-pointed: the old ``?tab=channels`` workspace-detail tab moved
        # into the Studio Settings modal (studio-settings.jsx), which renders
        # the SAME WS_ChannelsTab. Open it and drive the reused Link-channel
        # flow. The settings overlay is itself a ``workspace-settings`` modal,
        # so scope the Link-channel button to that panel.
        settings = open_workspace_settings(page, console_url, wid, "channels")
        link_btn = settings.get_by_role(
            "button", name="Link channel", exact=True,
        ).first
        expect(link_btn).to_be_visible(timeout=20_000)
        link_btn.click()

        # The Link-channel dialog renders on top of the settings overlay.
        # Scope to the modal that carries the channel <select> so we don't
        # clash with the outer settings modal (both share the .modal class).
        link_modal = page.locator(
            ".modal", has=page.locator("select.select")
        ).last
        expect(link_modal).to_be_visible(timeout=5_000)

        channel_select = link_modal.locator("select.select").first
        expect(
            channel_select.locator(f"option[value='{ch_id}']")
        ).to_be_attached(timeout=10_000)
        channel_select.select_option(ch_id)

        # Submit. The dialog's primary Btn label is "Link channel".
        link_modal.get_by_role("button", name="Link channel", exact=True).click()

        # The link-channel dialog closes; the workspace now shows the
        # channel as linked in the reused panel.
        expect(page.get_by_text(ch_id, exact=False).first).to_be_visible(
            timeout=15_000,
        )

        # Confirm the link landed server-side. The workspace's channel
        # link is now exposed as ``reply_binding`` (a WorkspaceChannelLink
        # carrying channel_id + anchor); the legacy ``channel_association``
        # field was renamed (back-compat alias only on read).
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            r = c.get(f"/v1/workspaces/{wid}")
            assert r.status_code == 200, r.text
            binding = r.json().get("reply_binding") or {}
            assert binding.get("channel_id") == ch_id, r.json()
    finally:
        _cleanup(base_url, ids, cleanup_cp, cleanup_ch)
