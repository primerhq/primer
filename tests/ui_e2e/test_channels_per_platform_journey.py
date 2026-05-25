"""UI E2E: §4/§5/§6 Channels per-platform create form + Probe disabled state.

Multi-page operator-journey that walks an operator authoring a brand-
new ChannelProvider across all three platforms (Slack / Telegram /
Discord) via the new-provider modal, then lands on the just-created
provider's detail page and confirms the Probe button is in its
documented "not yet implemented" state.

Pages traversed:
  /console/ (initial) → /channels/providers (list) → modal (3 platform
  switches) → submit → /channels/providers/{cp_id} (detail).

Subsystems exercised in one test:

  1. Provider list page mounts; "New provider" CTA enabled (no
     pre-existing provider required).
  2. NewChannelProviderModal — platform dropdown (Slack / Telegram /
     Discord). Switching the platform re-seeds the per-platform
     config fields via the React effect on `provider`.
  3. Per-platform field rendering:
       * Slack: app_token + bot_token + signing_secret password inputs.
       * Telegram: bot_token password + poll_timeout_seconds number
         input default=25.
       * Discord: bot_token password + enable_dms checkbox default=on.
  4. Submission of Discord form — POST /v1/channel_providers; success
     path navigates to the newly-created provider's detail page.
  5. Detail page renders the Probe button as `disabled` with the
     hint "Probe endpoint not yet implemented (backend follow-up)"
     per roadmap §8.

Covers backlog item U0111. Pins §4/§5/§6 per-platform field surfaces
+ §3 Probe-disabled-with-hint anomaly. All three platforms walked
in one function so a future refactor of CH_PROVIDER_FIELDS surfaces
mismatches across all three.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import expect


# 60-char Discord placeholder — matches the API-side check in
# DiscordChannelProviderConfig (>=30 chars).
_FAKE_DISCORD_TOKEN = "x" * 60


def _cleanup(base_url: str, cp_id: str) -> None:
    """Best-effort delete of the seeded provider."""
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        try:
            c.delete(f"/v1/channel_providers/{cp_id}")
        except Exception:  # noqa: BLE001
            pass


# ===========================================================================
# U0111 — Channels per-platform field rendering + Probe disabled state
# ===========================================================================


def test_u0111_channels_per_platform_create_form_journey(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
) -> None:
    """U0111 — Walk every platform's create-form fields, then submit
    the Discord variant and confirm the detail page's Probe button is
    disabled with the documented hint.

    Steps:

      1. Navigate /channels/providers → click "New provider" → modal
         opens (default platform=slack).
      2. Assert Slack fields visible: "App token", "Bot token",
         "Signing secret" — all 3 password inputs.
      3. Switch platform → telegram. Assert Telegram fields: "Bot
         token" password + "Poll timeout (s)" number input with the
         default value "25".
      4. Switch platform → discord. Assert Discord fields: "Bot
         token" password + "Enable DMs" checkbox (checked by default).
      5. Fill the Discord bot_token field with a 60-char placeholder;
         submit ("Create provider"). Modal closes; URL navigates to
         /channels/providers/{cp_id}.
      6. Detail page renders the Probe button — visible, disabled,
         carrying the documented hint title attribute.

    Pinned invariants:
      * Each platform's CH_PROVIDER_FIELDS schema renders the exact
        field set documented in channels.jsx:15-29.
      * Switching platforms re-seeds defaults (Telegram poll-timeout
        25, Discord enable_dms checked) via the useEffect on
        `provider`.
      * Probe button stays `disabled` with the roadmap §8 hint —
        regression net so the moment the backend endpoint lands,
        this test fails and forces a deliberate UI update.
    """
    cp_id = f"u111-cp-{unique_suffix}"

    try:
        # --- 1. Navigate /channels/providers → "New provider" -------
        page.goto(
            f"{console_url}#/channels/providers",
            wait_until="domcontentloaded",
        )
        new_btn = page.get_by_role(
            "button", name="New provider", exact=True,
        )
        expect(new_btn).to_be_visible(timeout=20_000)
        new_btn.click()

        modal = page.locator(".modal").first
        expect(modal).to_be_visible(timeout=5_000)

        # The "id" input is the first input in the modal (placeholder
        # "auto-generated"). Fill it with our deterministic id.
        modal.get_by_placeholder("auto-generated", exact=False).first.fill(cp_id)

        # --- 2. Slack fields visible (modal default) ----------------
        # Slack labels per CH_PROVIDER_FIELDS.slack in channels.jsx:16-20:
        # App token / Bot token / Signing secret.
        expect(modal.get_by_text("App token", exact=False)).to_be_visible(
            timeout=5_000,
        )
        expect(modal.get_by_text("Bot token", exact=False).first).to_be_visible(
            timeout=5_000,
        )
        expect(
            modal.get_by_text("Signing secret", exact=False),
        ).to_be_visible(timeout=5_000)
        # Three password inputs (app_token + bot_token + signing_secret
        # are all secret=true so type=password).
        slack_passwords = modal.locator("input[type=password]")
        expect(slack_passwords).to_have_count(3, timeout=5_000)

        # --- 3. Switch to Telegram --------------------------------
        platform_select = modal.locator("select.select").first
        platform_select.select_option("telegram")

        # Telegram fields: bot_token + poll_timeout_seconds (default 25).
        # ".first" because "Poll timeout" appears in both the
        # field label and the helper-text below; we only need one
        # visible to confirm the Telegram fields rendered.
        expect(
            modal.get_by_text("Poll timeout", exact=False).first,
        ).to_be_visible(timeout=5_000)
        # Bot token password input present.
        tg_passwords = modal.locator("input[type=password]")
        expect(tg_passwords).to_have_count(1, timeout=5_000)
        # poll_timeout_seconds is a number input; default = 25 per
        # CH_PROVIDER_FIELDS.telegram.poll_timeout_seconds.default.
        number_input = modal.locator("input[type=number]").first
        expect(number_input).to_have_value("25", timeout=5_000)
        # And NO signing_secret field — that's Slack-only.
        expect(
            modal.get_by_text("Signing secret", exact=False),
        ).to_have_count(0, timeout=5_000)

        # --- 4. Switch to Discord ---------------------------------
        platform_select.select_option("discord")

        # Discord fields: bot_token + enable_dms checkbox checked.
        # ".first" because "Enable DMs" appears in both the field
        # label and the inline checkbox label.
        expect(
            modal.get_by_text("Enable DMs", exact=False).first,
        ).to_be_visible(timeout=5_000)
        disc_passwords = modal.locator("input[type=password]")
        expect(disc_passwords).to_have_count(1, timeout=5_000)
        # Checkbox checked by default per
        # CH_PROVIDER_FIELDS.discord.enable_dms.default = true.
        enable_dms_box = modal.locator("input[type=checkbox]").first
        expect(enable_dms_box).to_be_checked(timeout=5_000)
        # No Poll timeout (Telegram-only) or Signing secret (Slack-only).
        expect(
            modal.get_by_text("Poll timeout", exact=False),
        ).to_have_count(0, timeout=5_000)
        expect(
            modal.get_by_text("Signing secret", exact=False),
        ).to_have_count(0, timeout=5_000)

        # --- 5. Submit Discord form -------------------------------
        disc_passwords.first.fill(_FAKE_DISCORD_TOKEN)
        submit_btn = modal.get_by_role(
            "button", name="Create provider", exact=True,
        )
        expect(submit_btn).to_be_enabled(timeout=5_000)
        submit_btn.click()

        # Modal closes + URL navigates to /channels/providers/{cp_id}
        expect(page.locator(".modal")).not_to_be_visible(timeout=10_000)
        page.wait_for_url(
            f"**/console/#/channels/providers/{cp_id}**", timeout=15_000,
        )

        # --- 6. Detail page Probe button disabled with hint --------
        probe_btn = page.get_by_role("button", name="Probe", exact=True).first
        expect(probe_btn).to_be_visible(timeout=15_000)
        expect(probe_btn).to_be_disabled()
        # The documented hint per roadmap §8 / channels.jsx:409.
        hint = probe_btn.get_attribute("title")
        assert hint and "Probe endpoint not yet implemented" in hint, (
            f"Probe button should carry the documented hint title; "
            f"got: {hint!r}"
        )
    finally:
        _cleanup(base_url, cp_id)
